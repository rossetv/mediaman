"""Filesystem operations — disk usage, deletion, size calculation."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger("mediaman")

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
        "/private/tmp",  # nosec B108 — forbidden delete root, not a temp path used by mediaman
        "/private/var",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/srv",
        "/sys",
        "/tmp",  # nosec B108 — listed as a *forbidden* delete root, not a temp path used by mediaman
        "/usr",
        "/var",
    }
)


def _validate_delete_roots(roots: list[str]) -> list[Path]:
    """Resolve and sanity-check the configured delete-root allowlist.

    Returns the list of resolved root paths, or raises :class:`ValueError`
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
        raise ValueError(
            "delete_allowed_roots not configured; refusing deletion. "
            "Set the delete_allowed_roots setting (JSON list) or the "
            "MEDIAMAN_DELETE_ROOTS env var (colon-separated)."
        )
    resolved: list[Path] = []
    for raw in roots:
        if not raw or not isinstance(raw, str):
            raise ValueError(
                "Refusing to delete: an entry in delete_allowed_roots is "
                "empty or not a string. Configure delete_allowed_roots "
                "with absolute directory paths only."
            )
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise ValueError(
                f"Refusing to delete: allowed root '{raw}' is not an "
                "absolute path. Configure delete_allowed_roots with "
                "absolute directory paths only."
            )
        # Resolve to the canonical form first so the forbidden-root
        # check catches things like ``/data/..`` (which resolves to
        # ``/``) regardless of whether the literal path exists. We
        # tolerate a missing path here — operators sometimes configure
        # roots that don't exist yet (e.g. a mount that's brought up
        # later) and the misconfiguration is still caught by the
        # forbidden-root list.
        try:
            real = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                f"Refusing to delete: allowed root '{raw}' could not be resolved: {exc}"
            ) from exc
        normalised = str(real)
        if normalised in _FORBIDDEN_ROOTS:
            raise ValueError(
                f"Refusing to delete: allowed root '{raw}' resolves to "
                f"'{normalised}', a system / mount-root path that must not "
                "be a delete root. Configure delete_allowed_roots with "
                "specific content directories (e.g. '/media/movies'), "
                "never bare top-level mounts."
            )
        # Atomic symlink check via ``O_NOFOLLOW``: if the candidate
        # path exists, open it with O_NOFOLLOW so the kernel refuses
        # the open if the leaf is a symlink. The earlier two-step
        # ``is_symlink()`` then ``resolve()`` had a TOCTOU window in
        # which an attacker could swap the directory for a symlink
        # between syscalls; the fd-based form rejects that. If the
        # path is missing entirely we accept it (matches previous
        # behaviour for unconfigured-yet mounts) — the forbidden-root
        # check above already shielded the dangerous cases.
        if real.exists():
            try:
                fd = os.open(
                    str(candidate),
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
                )
            except OSError as exc:
                import errno as _errno

                # ``O_NOFOLLOW`` on a symlink yields ``ELOOP`` on Linux
                # but ``ENOTDIR`` (and sometimes ``ELOOP``) on macOS,
                # depending on whether ``O_DIRECTORY`` or the symlink
                # rule fires first. Re-check with ``lstat`` to give a
                # precise error in either case.
                try:
                    lst = os.lstat(str(candidate))
                except OSError:
                    lst = None
                import stat as _stat

                if lst is not None and _stat.S_ISLNK(lst.st_mode):
                    raise ValueError(
                        f"Refusing to delete: allowed root '{raw}' is a symlink. "
                        "Configure delete_allowed_roots with real directories only."
                    ) from None
                if exc.errno == _errno.ELOOP:
                    raise ValueError(
                        f"Refusing to delete: allowed root '{raw}' is a symlink. "
                        "Configure delete_allowed_roots with real directories only."
                    ) from None
                if exc.errno == _errno.ENOTDIR:
                    raise ValueError(
                        f"Refusing to delete: allowed root '{raw}' is not a directory."
                    ) from None
                # Anything else (EACCES, etc.) — operator config error.
                raise ValueError(
                    f"Refusing to delete: allowed root '{raw}' cannot be opened: {exc}"
                ) from exc
            else:
                os.close(fd)
        resolved.append(real)
    return resolved


def get_aggregate_disk_usage(base_path: str) -> dict[str, int]:
    """Return combined disk usage across all unique mount points under *base_path*.

    Detects subdirectories on different devices (e.g. a separate disk mounted
    at ``/media/movies`` under ``/media``) and sums their usage so the total
    reflects all physical disks, not just the root mount.
    """
    if not Path(base_path).exists():
        raise FileNotFoundError(f"Path does not exist: {base_path}")

    seen_devices: set[int] = set()
    total = 0
    used = 0
    free = 0

    def _add_path(p: str) -> None:
        nonlocal total, used, free
        dev = os.stat(p).st_dev
        if dev in seen_devices:
            return
        seen_devices.add(dev)
        usage = shutil.disk_usage(p)
        total += usage.total
        used += usage.used
        free += usage.free

    _add_path(base_path)
    try:
        for entry in os.scandir(base_path):
            if entry.is_dir(follow_symlinks=False):
                _add_path(entry.path)
    except PermissionError:
        logger.debug("Cannot scandir %s — skipping subdirectory enumeration", base_path)

    return {"total_bytes": total, "used_bytes": used, "free_bytes": free}


def delete_path(path: str, *, allowed_roots: list[str] | None = None) -> None:
    """Delete a file or directory, with mandatory path validation.

    ``allowed_roots`` is required and must be a non-empty list. Raises
    ``ValueError`` if it is missing or empty, if any root is itself a
    forbidden top-level path (``/``, ``/etc``, ``/data``, etc.), or if
    the resolved target is not a *strict descendant* of one of the roots.

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
        raise ValueError(
            "delete_path requires allowed_roots — refusing deletion until "
            "a trusted allowlist is supplied."
        )
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError(
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
        raise ValueError(
            f"Refusing to delete '{p}' — must be a strict descendant of "
            f"an allowed root, not the root itself. Allowed roots: "
            f"{[str(r) for r in resolved_roots]}"
        )
    # Refuse if the caller's path itself is a symlink — resolving
    # follows it, so the containment check above was against the
    # target, not the link. A symlink passed as the deletion target is
    # always suspicious regardless of where it points.
    if raw.is_symlink():
        raise ValueError(f"Refusing to delete '{raw}' — target path is a symlink.")
    _safe_rmtree(p, resolved_roots, original_allowed_roots=allowed_roots)


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
        raise ValueError(
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
            raise ValueError(
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
        raise ValueError(f"Refusing to delete '{resolved}' — target is a symlink.")

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
        raise ValueError(f"Refusing to delete '{resolved}' — no matching allowed root found.")
    pinned_root = max(matching_roots, key=lambda r: len(str(r)))
    try:
        root_dev = os.stat(str(pinned_root)).st_dev
    except FileNotFoundError as exc:
        raise ValueError(
            f"Refusing to delete '{resolved}' — allowed root '{pinned_root}' no longer exists."
        ) from exc

    # Must live on the same device as the root (defeats mount-swap).
    if lst.st_dev != root_dev:
        raise ValueError(
            f"Refusing to delete '{resolved}' — different device from allowed root '{pinned_root}'."
        )

    import stat as _stat

    if _stat.S_ISREG(lst.st_mode):
        os.unlink(str(resolved))
        return
    if not _stat.S_ISDIR(lst.st_mode):
        raise ValueError(f"Refusing to delete '{resolved}' — not a regular file or directory.")

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
                raise ValueError(
                    f"Refusing to delete '{resolved}' — cannot stat '{dirpath}': {exc}"
                ) from exc
            if dev != root_dev:
                raise ValueError(
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
                    raise ValueError(
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
                    raise ValueError(
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


def get_directory_size(path: str) -> int:
    """Return the total size in bytes of all regular files under *path*."""
    total = 0
    for fp in Path(path).rglob("*"):
        if fp.is_file():
            total += fp.stat().st_size
    return total
