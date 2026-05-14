"""TOCTOU-hardened filesystem deletion operations.

This module owns the destructive operations that consume the validated
delete-root allowlist: the public :func:`delete_path` entry point and the
:func:`_safe_rmtree` recursive remover. The allowlist *validation* helpers
(forbidden-root refusal, atomic symlink check) live in :mod:`._delete_roots`.

Threat model: between the caller's containment check and the actual removal
there is a TOCTOU window. An attacker who can swap a directory for a symlink
in that window could redirect the deletion at ``/``. :func:`_safe_rmtree`
closes the window by re-resolving and re-checking containment at delete time,
pinning to the root's filesystem device, and walking with fd-based
``os.fwalk(follow_symlinks=False)`` so no swapped-in symlink is ever crossed.
"""

from __future__ import annotations

import os
import stat as _stat
from pathlib import Path

from mediaman.services.infra.storage._delete_roots import (
    DeletionRefused,
    _validate_delete_roots,
)


def delete_path(path: str, *, allowed_roots: list[str] | None = None) -> None:
    """Delete a file or directory, with mandatory path validation.

    ``allowed_roots`` is required and must be a non-empty list. Raises
    :class:`DeletionRefused` if it is missing or empty, if any root is
    itself a forbidden top-level path (``/``, ``/etc``, ``/data``, etc.),
    or if the resolved target is not a *strict descendant* of one of the
    roots.

    The strict-descendant rule is critical: if ``allowed_roots == ["/media"]``
    we must refuse a target equal to ``/media`` itself, because a
    compromised or buggy Plex response can populate ``part.file = "/media"``
    and the cleanup job would otherwise recursively wipe the entire mount.
    Only paths *under* a configured root are eligible.

    The target *path* itself must be absolute. A relative path resolves
    against the current working directory, which is implementation
    detail nothing legitimate should rely on for a destructive
    operation; we refuse outright rather than silently anchor the
    deletion at CWD.
    """
    if allowed_roots is None:
        raise DeletionRefused(
            "delete_path requires allowed_roots — refusing deletion until "
            "a trusted allowlist is supplied."
        )
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise DeletionRefused(
            f"Refusing to delete '{path}' — target must be an absolute path. "
            "Relative paths anchor on the current working directory which "
            "is unsafe for a destructive operation."
        )
    # Validate the allowlist fully before we touch the target — bad
    # config must never be ignored just because the caller's path is
    # innocuous.
    resolved_roots = _validate_delete_roots(allowed_roots)
    raw = Path(path)
    p = raw.resolve()
    # Strict descendant only — never accept ``p == root``. A delete
    # target that *is* the root would let an attacker (or buggy Plex
    # response) rmtree the entire allowlisted mount.
    if not any(root in p.parents for root in resolved_roots):
        raise DeletionRefused(
            f"Refusing to delete '{p}' — must be a strict descendant of "
            f"an allowed root, not the root itself. Allowed roots: "
            f"{[str(r) for r in resolved_roots]}"
        )
    # Refuse if the caller's path itself is a symlink — resolving
    # follows it, so the containment check above was against the
    # target, not the link. A symlink passed as the deletion target is
    # always suspicious regardless of where it points.
    if raw.is_symlink():
        raise DeletionRefused(f"Refusing to delete '{raw}' — target path is a symlink.")
    _safe_rmtree(p, resolved_roots, original_allowed_roots=allowed_roots)


# rationale: symlink-resolution, containment check, recursive descent, and
# deletion are interleaved so that each symlink decision is made atomically
# with the stat that revealed it — splitting into pre-check and delete phases
# would reintroduce the TOCTOU symlink-swap window this function was designed
# to close.
def _safe_rmtree(
    path: Path,
    allowed_roots: list[Path],
    *,
    original_allowed_roots: list[str] | None = None,
) -> None:
    """Delete *path* without following symlinks or escaping *allowed_roots*.

    Hardens against TOCTOU / symlink-swap attacks between the caller's
    containment check and the actual removal:

    * Refuses to descend into any symlinked directory — target, an
      allowed root that is itself a symlink, or any nested entry found
      mid-walk.
    * Re-resolves *path* and re-checks containment at delete time,
      rather than trusting the caller's earlier resolve.
    * Stays on the same filesystem device as the resolved root so a
      mount swapped in mid-walk cannot redirect deletions.
    * Uses ``os.fwalk(..., follow_symlinks=False)`` so even if a
      directory is swapped for a symlink after our initial stat, the
      walk's file-descriptor-based rmdir/unlink refuses to cross it.

    Non-existent paths are a silent no-op (parity with the previous
    implementation). A single *file* target is unlinked after the same
    symlink / containment checks.
    """
    # Re-resolve now, don't trust an earlier resolve. Any component that
    # was a symlink at check time but a real directory now (or vice
    # versa) is caught here.
    resolved = path.resolve()
    # Strict-descendant re-check — same rule as delete_path's own check
    # so a TOCTOU swap that leaves the target equal to a configured
    # root is still refused.
    if not any(root in resolved.parents for root in allowed_roots):
        raise DeletionRefused(
            f"Refusing to delete '{resolved}' — must be a strict "
            "descendant of an allowed root on re-check. Allowed roots: "
            f"{[str(r) for r in allowed_roots]}"
        )

    # The caller (delete_path) has already validated the allowlist and
    # rejected any symlinked / forbidden roots — we don't repeat that
    # work here, but we leave the parameter in place for callers that
    # bypass delete_path (and hence need the extra defence).
    for raw_root in original_allowed_roots or []:
        if Path(raw_root).is_symlink():
            raise DeletionRefused(
                f"Refusing to delete: allowed root '{raw_root}' is a symlink. "
                "Configure delete_allowed_roots with real directories only."
            )

    # Missing: silent no-op (matches previous behaviour).
    try:
        lst = os.lstat(str(resolved))
    except FileNotFoundError:
        return

    # Target itself must not be a symlink — otherwise an attacker could
    # swap the intended directory for a link to / after the containment
    # check.
    if os.path.islink(str(resolved)):
        raise DeletionRefused(f"Refusing to delete '{resolved}' — target is a symlink.")

    # Identify which root we're rooted under so we can pin to its
    # device. When two roots share a parent (e.g. ``/media`` and
    # ``/media/movies`` mounted on a separate device), the *longest*
    # matching root is the correct anchor — picking the more general
    # one would compare the target's device against the umbrella mount
    # rather than the actual content mount and refuse the deletion as
    # cross-device.
    matching_roots = [root for root in allowed_roots if root in resolved.parents]
    if not matching_roots:
        # Should be unreachable — ``delete_path`` already enforced the
        # strict-descendant rule. Belt-and-braces nonetheless.
        raise DeletionRefused(f"Refusing to delete '{resolved}' — no matching allowed root found.")
    pinned_root = max(matching_roots, key=lambda r: len(str(r)))
    try:
        root_dev = os.stat(str(pinned_root)).st_dev
    except FileNotFoundError as exc:
        raise DeletionRefused(
            f"Refusing to delete '{resolved}' — allowed root '{pinned_root}' no longer exists."
        ) from exc

    # Must live on the same device as the root (defeats mount-swap).
    if lst.st_dev != root_dev:
        raise DeletionRefused(
            f"Refusing to delete '{resolved}' — different device from allowed root '{pinned_root}'."
        )

    if _stat.S_ISREG(lst.st_mode):
        os.unlink(str(resolved))
        return
    if not _stat.S_ISDIR(lst.st_mode):
        raise DeletionRefused(f"Refusing to delete '{resolved}' — not a regular file or directory.")

    # Walk bottom-up, never following symlinks. Every entry is checked
    # against the root device and must not be a symlink before we unlink
    # or rmdir it. ``os.fwalk`` gives us a dir fd per step so the rm
    # operations are relative to the opened directory, not re-resolved
    # from the original path — that's what defeats a symlink swap
    # between our lstat and our unlink.
    #
    # ``os.fwalk`` is a generator that holds open file descriptors at
    # each level. If we raise mid-iteration the generator stays alive
    # until garbage-collection runs, leaking those fds for the duration.
    # Wrap the iteration in a try/finally that explicitly closes the
    # generator so the fds are released as soon as we bail out.
    walker = os.fwalk(str(resolved), topdown=False, follow_symlinks=False)
    try:
        for dirpath, dirnames, filenames, dirfd in walker:
            # Refuse to descend into any device other than the pinned root's.
            try:
                dev = os.fstat(dirfd).st_dev
            except OSError as exc:
                raise DeletionRefused(
                    f"Refusing to delete '{resolved}' — cannot stat '{dirpath}': {exc}"
                ) from exc
            if dev != root_dev:
                raise DeletionRefused(
                    f"Refusing to delete '{resolved}' — '{dirpath}' is on a "
                    "different device than the allowed root."
                )

            for name in filenames:
                entry_stat = os.lstat(name, dir_fd=dirfd)
                if _stat.S_ISLNK(entry_stat.st_mode):
                    # Remove symlink entries themselves (they don't follow
                    # the link to delete its target) — this is safe and the
                    # only way to empty the dir.
                    os.unlink(name, dir_fd=dirfd)
                    continue
                if entry_stat.st_dev != root_dev:
                    raise DeletionRefused(
                        f"Refusing to delete '{resolved}' — entry '{name}' in "
                        f"'{dirpath}' is on a different device."
                    )
                os.unlink(name, dir_fd=dirfd)

            for name in dirnames:
                entry_stat = os.lstat(name, dir_fd=dirfd)
                if _stat.S_ISLNK(entry_stat.st_mode):
                    # Symlinked subdir: remove the link entry, don't recurse
                    # (fwalk already refuses with follow_symlinks=False).
                    os.unlink(name, dir_fd=dirfd)
                    continue
                if entry_stat.st_dev != root_dev:
                    raise DeletionRefused(
                        f"Refusing to delete '{resolved}' — subdirectory "
                        f"'{name}' in '{dirpath}' is on a different device."
                    )
                os.rmdir(name, dir_fd=dirfd)
    finally:
        # ``close()`` on a generator runs its finally blocks (which
        # close the open fds) immediately rather than waiting for GC.
        # mypy types ``os.fwalk`` as ``Iterator``, but the concrete
        # return value is a generator which always has ``close()``.
        walker.close()  # type: ignore[attr-defined]

    # Finally remove the top-level directory itself.
    os.rmdir(str(resolved))
