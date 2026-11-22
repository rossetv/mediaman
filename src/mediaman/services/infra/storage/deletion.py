"""TOCTOU-hardened filesystem deletion operations.

This module owns the destructive operations that consume the validated
delete-root allowlist: the public ``delete_path`` entry point and its
private ``_safe_rmtree`` recursive remover (not exported from the package
barrel — ``delete_path`` is the only public way to trigger a recursive
deletion), and the narrower ``delete_tracked_files`` (an item's own files and
their sidecars) with ``remove_empty_folders`` (the folders those leave bare). The
allowlist *validation* helpers (forbidden-root refusal, atomic symlink check)
live in ``_delete_roots``.

Threat model: between the caller's containment check and the actual removal
there is a TOCTOU window. An attacker who can swap a directory for a symlink
in that window could redirect the deletion at ``/``. ``_safe_rmtree``
narrows the window by re-resolving and re-checking containment at delete time,
pinning to the root's filesystem device (checked *before* the per-entry
symlink branch so a cross-device swap is refused regardless), and walking with
fd-based ``os.fwalk(follow_symlinks=False)`` so no swapped-in directory symlink
is ever crossed. The per-entry ``lstat``-then-``unlink`` on a name is two
syscalls, not one atomic operation — ``os.unlink`` never follows a final
symlink, so the link target is safe, but this is defence-in-depth, not a
claim of per-entry atomicity. See the ``# rationale:`` block on
``_safe_rmtree`` for the precise scope of the guarantee.
"""

from __future__ import annotations

import os
import re
import stat as _stat
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mediaman.services.infra.storage._delete_roots import (
    DeletionRefused,
    _is_forbidden_root,
    _validate_delete_roots,
)


def path_within_delete_roots(path: str, allowed_roots: list[str]) -> bool:
    """Return True iff *path* is a strict descendant of a validated delete root.

    A pure containment predicate — it performs **no** filesystem mutation
    and never raises. Use it to decide whether a stored path *would have
    been* deletable by ``delete_path`` when the file itself is already
    absent (so the device / symlink checks in ``_safe_rmtree`` cannot
    run). The crash-recovery path uses it to refuse crediting "freed bytes"
    for a path that was never inside the configured roots.

    Returns ``False`` (fail-closed) when *path* is empty, not absolute, the
    allowlist is empty or malformed (any forbidden / relative / symlink
    root), or *path* resolves to a root itself rather than a strict
    descendant. The strict-descendant rule matches ``delete_path``
    exactly so the predicate and the executor never disagree.
    """
    if not path or not isinstance(path, str):
        return False
    raw = Path(path)
    if not raw.is_absolute():
        return False
    try:
        resolved_roots = _validate_delete_roots(allowed_roots)
    except DeletionRefused:
        return False
    # resolve() on a missing path simply normalises it (there is no
    # symlink on disk to follow), which is the correct, conservative
    # behaviour for the file-absent recovery case.
    resolved = raw.resolve()
    return any(root in resolved.parents for root in resolved_roots)


def unavailable_folder(path: str, allowed_roots: list[str]) -> str | None:
    """Return the folder found missing or empty above *path*, or None if it has content.

    The folder looked at is the closest existing one above *path*, or the
    delete root holding *path* when that is missing; the search stops at that
    root. A media mount that has dropped leaves its mount point missing or
    empty, and every target under it then looks deleted. A caller that finds
    its target absent asks this first, so it does not book an unmounted file
    as removed. Roots and *path* are resolved as ``path_within_delete_roots``
    resolves them, so a root reached through a symlink or spelled another way
    still matches. Never raises; any doubt (no matching root, unreadable
    folder) names a folder, or *path* itself when there is none to name. It
    does not vet the allowlist: ``delete_path`` does that.
    """
    if not path or not isinstance(path, str) or not os.path.isabs(path):
        return str(path)
    roots: list[Path] = []
    try:
        target = Path(path).resolve()
        for raw in allowed_roots:
            if raw and isinstance(raw, str) and os.path.isabs(raw):
                root = Path(raw).resolve()
                if root in target.parents:
                    roots.append(root)
    except (OSError, RuntimeError):
        return path
    if not roots:
        return path
    pinned_root = max(roots, key=lambda root: len(root.parts))
    # A separate mount below the root that has dropped leaves an empty mount
    # point while the root itself still has content, so look at the closest
    # folder that still exists above the target.
    closest = next(
        folder for folder in target.parents if folder == pinned_root or os.path.lexists(folder)
    )
    try:
        with os.scandir(closest) as entries:
            if next(entries, None) is not None:
                return None
    except OSError:
        pass
    return str(closest)


def delete_path(path: str, *, allowed_roots: list[str] | None = None) -> None:
    """Delete a file or directory, with mandatory path validation.

    ``allowed_roots`` is required and must be a non-empty list. Raises
    ``DeletionRefused`` if it is missing or empty, if any root is
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
    _safe_rmtree(p, resolved_roots)


# rationale: symlink-resolution, containment check, recursive descent, and
# deletion are interleaved so each symlink/device decision is made as close as
# possible to the stat that revealed it, and the whole walk runs under
# ``os.fwalk(follow_symlinks=False)`` dir file descriptors so the unlink/rmdir
# operations are relative to an *opened* directory rather than a re-resolved
# path. Splitting into separate pre-check and delete phases would reintroduce
# the TOCTOU symlink-swap window this function was designed to close.
#
# Honest scope of the guarantee: per-entry the sequence is ``os.lstat(name,
# dir_fd=...)`` then ``os.unlink/rmdir(name, dir_fd=...)`` — two syscalls on a
# *name*, not one atomic operation on a held inode fd. ``os.unlink`` never
# follows a final symlink, so the link's target is never deleted; and the
# fwalk dir fd means a swapped-in *directory* cannot redirect the walk. The
# residual race is narrow (a name swapped between the lstat and the unlink),
# and the same-device check now runs before the symlink branch so a cross-
# device swap is still refused. We deliberately do NOT claim each unlink is
# atomic with its stat — closing that fully would require an
# ``os.open(O_NOFOLLOW|O_PATH)`` + ``fstat`` + unlink-under-fd rewrite, which
# is not implemented here.
def _safe_rmtree(path: Path, allowed_roots: list[Path]) -> None:
    """Delete *path* without following symlinks or escaping *allowed_roots*.

    NOT a public entry point. ``delete_path`` is the only supported way
    in — ``_safe_rmtree`` is deliberately absent from the package barrel's
    ``__all__`` so a caller cannot reach it without going through
    ``delete_path``'s full allowlist validation. The defences below are
    re-checked here regardless, so a direct in-module call is still safe.

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

    Non-existent paths are a silent no-op (there is nothing
    to delete). A single *file* target is unlinked after the same
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

    # Reject any allowed root that is a symlink on disk — checked
    # UNCONDITIONALLY, re-derived from the ``allowed_roots`` this
    # function actually walks under rather than an optional raw-roots
    # parameter a bypassing caller might omit. The primary defence is
    # that ``_safe_rmtree`` is not in the package barrel's ``__all__``,
    # so the only public path in is ``delete_path``, which already rejects
    # symlinked raw roots via ``_validate_delete_roots``. This loop is
    # belt-and-braces for the in-module call: if a root path is *currently*
    # a symlink it must not be walked under, because the device pin below
    # would otherwise follow it onto another filesystem.
    for root in allowed_roots:
        if os.path.islink(str(root)):
            raise DeletionRefused(
                f"Refusing to delete: allowed root '{root}' is a symlink. "
                "Configure delete_allowed_roots with real directories only."
            )

    # Missing: silent no-op.
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
                # Device check FIRST — before the symlink short-circuit — so a
                # cross-device entry is refused whether or not it is a symlink.
                # A symlink entry swapped in for a hardlink-to-another-
                # device file must still trip the device guard.
                if entry_stat.st_dev != root_dev:
                    raise DeletionRefused(
                        f"Refusing to delete '{resolved}' — entry '{name}' in "
                        f"'{dirpath}' is on a different device."
                    )
                if _stat.S_ISLNK(entry_stat.st_mode):
                    # Remove symlink entries themselves (``os.unlink`` does not
                    # follow the final link to its target) — this is safe and
                    # the only way to empty the dir.
                    os.unlink(name, dir_fd=dirfd)
                    continue
                os.unlink(name, dir_fd=dirfd)

            for name in dirnames:
                entry_stat = os.lstat(name, dir_fd=dirfd)
                # Device check FIRST (see filenames loop above) — a cross-
                # device subdir entry is refused even if it is a symlink.
                if entry_stat.st_dev != root_dev:
                    raise DeletionRefused(
                        f"Refusing to delete '{resolved}' — subdirectory "
                        f"'{name}' in '{dirpath}' is on a different device."
                    )
                if _stat.S_ISLNK(entry_stat.st_mode):
                    # Symlinked subdir: remove the link entry, don't recurse
                    # (fwalk already refuses with follow_symlinks=False).
                    os.unlink(name, dir_fd=dirfd)
                    continue
                os.rmdir(name, dir_fd=dirfd)
    finally:
        # ``close()`` on a generator runs its finally blocks (which
        # close the open fds) immediately rather than waiting for GC.
        # mypy types ``os.fwalk`` as ``Iterator``, but the concrete
        # return value is a generator which always has ``close()``.
        walker.close()  # type: ignore[attr-defined]

    # Finally remove the top-level directory itself.
    os.rmdir(str(resolved))


_SIDECAR_EXTENSIONS = frozenset({"srt", "ass", "ssa", "sub", "idx", "vtt", "nfo"})
_SUBTITLE_FLAGS = frozenset({"forced", "sdh", "cc", "hi", "default", "foreign"})
# ISO 639-1, then the common ISO 639-2 codes. The bibliographic codes that are
# also English words (may, per, ice, ...) are left out; their terminology
# codes (msa, fas, isl, ...) stand in.
_ISO_639_CODES = """
    aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch
    co cr cs cu cv cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga
    gd gl gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja
    jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv
    mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or
    os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr
    ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi
    vo wa wo xh yi yo za zh zu
    afr amh ara aze bos bul cat ces chi cym cze dan deu dut ell eng est eus fas
    fil fin fra fre ger gle glg gre guj heb hin hrv hun hye ind isl ita jpn kan
    kat kaz khm kor lao lav lit mal mkd msa mya nep nld nno nob nor pol por ron
    rus slk slv spa sqi srp swa swe tel tgl tha tur ukr und urd uzb vie yue zho
"""
_LANGUAGE_CODES = frozenset(_ISO_639_CODES.split())
# A language code with an optional region ("pt-BR", "es-419") or script ("zh-Hans").
_LANGUAGE_TAG = re.compile(r"([a-z]{2,3})(?:[-_](?:[a-z]{2}|[0-9]{3}|[a-z]{4}))?", re.IGNORECASE)
_ARTWORK = re.compile(
    r"(?:poster|fanart|folder|banner|landscape|clearlogo|clearart|thumb|discart"
    r"|season[a-z0-9-]*-(?:poster|banner|fanart))\.(?:jpg|png)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TrackedFilesResult:
    """What ``delete_tracked_files`` did.

    ``removed`` lists every file unlinked (tracked files and their sidecars),
    ``bytes_freed`` sums only their sizes, ``not_found`` lists the tracked
    files that were not on disk, ``skipped_protected`` the tracked files
    kept because another item tracks them, and ``failed`` each file whose
    removal failed, with the reason.
    """

    removed: list[str]
    bytes_freed: int
    not_found: list[str]
    skipped_protected: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def delete_tracked_files(
    tracked_files: list[dict[str, Any]],
    *,
    allowed_roots: list[str],
    protected_paths: Collection[str] = frozenset(),
    sweep_sidecars_of: Collection[str] = frozenset(),
) -> TrackedFilesResult:
    """Remove each tracked file of an item and its same-stem sidecars, nothing else.

    *tracked_files* holds ``{"path", "size"}`` entries. Every path is checked
    before anything is removed, by the same rules as ``delete_path`` (absolute,
    strict descendant of a validated root, not a symlink, on the root's
    device) and it must be a regular file; any failure raises
    ``DeletionRefused`` with nothing removed. Each removal re-checks the root,
    device and file type at unlink time; a file that fails then is reported in
    ``failed`` (with its sidecars left alone) and the others still go.

    A sidecar is a regular file beside the video named ``<stem>.<tags>.<ext>``
    with only language (ISO 639) or flag tags and a subtitle or ``.nfo``
    extension. Any other file in the folder whose extension is not a sidecar
    one owns the sidecars of its stem: while one with the video's own stem
    remains (``Film.mp4`` beside ``Film.mkv``) no sidecar is removed, and a
    longer stem claims its own (``Alien.Two.srt`` beside ``Alien.Two.mts``
    belongs to that file). A path in
    *protected_paths* (tracked by other items) is never removed, whether it
    is spelled as given or reached through a symlinked folder: a protected
    tracked file is kept, with its sidecars, and reported in
    ``skipped_protected``. A tracked file that is missing is reported in
    ``not_found`` and its sidecars are left alone, unless its path is in
    *sweep_sidecars_of*: a video already removed whose sidecars may have
    failed to go, so they are looked for and removed by the same rules. A
    video whose folder cannot be listed when its sidecars are looked for, or
    is missing while ``unavailable_folder`` finds its mount down, is reported
    in ``failed``.
    """
    roots = _validate_delete_roots(allowed_roots)
    targets = [_checked_tracked_path(entry, roots) for entry in tracked_files]
    protected = _protected_by_name(protected_paths)
    sweep = {os.path.normpath(path) for path in sweep_sidecars_of}
    removed: list[str] = []
    not_found: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []
    freed = 0
    seen: set[Path] = set()
    for target in targets:
        try:
            real = target.parent.resolve() / target.name
        except (OSError, RuntimeError) as exc:
            failed.append((str(target), str(exc)))
            continue
        if real in seen:
            continue
        seen.add(real)
        if _is_protected(target, protected):
            skipped.append(str(target))
            continue
        try:
            size = _unlink_regular_file(target, roots)
        except (DeletionRefused, OSError) as exc:
            failed.append((str(target), str(exc)))
            continue
        if size is None:
            not_found.append(str(target))
            if os.path.normpath(target) not in sweep:
                continue
        else:
            removed.append(str(target))
            freed += size
        try:
            sidecars = _sidecars_of(target, protected)
        except OSError as exc:
            # A folder gone while its delete root still has content took its
            # sidecars with it (an earlier pass may have removed it bare); a
            # dropped mount or an unreadable folder cannot show them gone.
            if (
                not isinstance(exc, FileNotFoundError)
                or unavailable_folder(str(target), allowed_roots) is not None
            ):
                failed.append((str(target), f"its folder could not be listed: {exc}"))
            continue
        for sidecar in sidecars:
            try:
                size = _unlink_regular_file(sidecar, roots)
            except (DeletionRefused, OSError) as exc:
                failed.append((str(sidecar), str(exc)))
                continue
            if size is not None:
                removed.append(str(sidecar))
                freed += size
    return TrackedFilesResult(
        removed=removed,
        bytes_freed=freed,
        not_found=not_found,
        skipped_protected=skipped,
        failed=failed,
    )


def remove_empty_folders(
    removed_files: list[str], *, allowed_roots: list[str], boundaries: Mapping[str, str]
) -> list[str]:
    """Remove the folders *removed_files* left bare, walking up; return those removed.

    A folder is bare when it holds nothing, or only regular files that are
    standard artwork (``poster.jpg``, ``season01-banner.png`` and the like) or
    ``.nfo`` files; those files are removed first, then the folder. The walk
    stops at the first folder that is not bare, never removes a delete root,
    a folder above one or a denied folder, and stops on any symlink,
    subfolder or other device. A folder reached through a symlinked parent is
    skipped. Raises ``DeletionRefused`` only for a bad allowlist.

    *boundaries* maps each path in *removed_files* to the highest folder its
    walk may remove: the film's own folder, or the series folder for a
    season. A boundary that is missing, not strictly inside a delete root,
    or not above the file limits that walk to the file's own folder.
    """
    roots = _validate_delete_roots(allowed_roots)
    limits: dict[Path, Path] = {}
    for removed_file in removed_files:
        parent = Path(removed_file).parent
        if not parent.is_absolute() or os.path.islink(parent):
            continue
        start = parent.resolve()
        limit = _walk_limit(start, boundaries.get(removed_file), roots)
        current = limits.get(start)
        # Files sharing a folder but given different boundaries walk only as
        # far as the lower one allows.
        if current is None or len(limit.parts) > len(current.parts):
            limits[start] = limit
    removed: list[str] = []
    for folder, limit in limits.items():
        while _remove_bare_folder(folder, roots):
            removed.append(str(folder))
            if folder == limit:
                break
            folder = folder.parent
    return removed


def _walk_limit(start: Path, boundary: object, roots: list[Path]) -> Path:
    """Return *boundary* resolved when it is a valid limit for a walk from *start*, else *start*."""
    if not isinstance(boundary, str) or not os.path.isabs(boundary):
        return start
    try:
        limit = Path(boundary).resolve()
    except (OSError, RuntimeError):
        return start
    if limit in roots or not any(root in limit.parents for root in roots):
        return start
    if limit != start and limit not in start.parents:
        return start
    return limit


def _pinned_root(resolved: Path, roots: list[Path]) -> Path:
    """Return the longest root *resolved* strictly descends from, or refuse."""
    matching = [root for root in roots if root in resolved.parents]
    if not matching:
        raise DeletionRefused(
            f"Refusing to delete '{resolved}' — must be a strict descendant of "
            f"an allowed root. Allowed roots: {[str(r) for r in roots]}"
        )
    return max(matching, key=lambda root: len(root.parts))


def _checked_tracked_path(entry: object, roots: list[Path]) -> Path:
    """Return the tracked file's path once it passes ``delete_path``'s checks."""
    path = entry.get("path") if isinstance(entry, dict) else None
    if not isinstance(path, str) or not os.path.isabs(path):
        raise DeletionRefused(f"Refusing to delete tracked file {entry!r} — not an absolute path.")
    raw = Path(path)
    if raw.name in ("", ".", ".."):
        raise DeletionRefused(f"Refusing to delete '{raw}' — not a file path.")
    real = raw.parent.resolve() / raw.name
    pinned = _pinned_root(real, roots)
    try:
        lst = os.lstat(real)
    except FileNotFoundError:
        return raw
    if _stat.S_ISLNK(lst.st_mode) or raw.is_symlink():
        raise DeletionRefused(f"Refusing to delete '{raw}' — target path is a symlink.")
    if not _stat.S_ISREG(lst.st_mode):
        raise DeletionRefused(f"Refusing to delete '{raw}' — not a regular file.")
    _require_root_device(real, lst, pinned)
    return raw


def _require_root_device(real: Path, lst: os.stat_result, pinned: Path) -> None:
    """Refuse unless *lst* (the stat of *real*) is on the same device as root *pinned*."""
    try:
        root_dev = os.stat(pinned).st_dev
    except FileNotFoundError as exc:
        raise DeletionRefused(
            f"Refusing to delete '{real}' — allowed root '{pinned}' no longer exists."
        ) from exc
    if lst.st_dev != root_dev:
        raise DeletionRefused(
            f"Refusing to delete '{real}' — different device from allowed root '{pinned}'."
        )


def _unlink_regular_file(path: Path, roots: list[Path]) -> int | None:
    """Unlink the regular file at *path* and return its size, or None if it is missing.

    Only the parent is resolved, so a final component swapped for a symlink
    is seen by ``lstat`` and refused rather than followed.
    """
    real = path.parent.resolve() / path.name
    pinned = _pinned_root(real, roots)
    try:
        lst = os.lstat(real)
    except FileNotFoundError:
        return None
    _require_root_device(real, lst, pinned)
    if not _stat.S_ISREG(lst.st_mode):
        raise DeletionRefused(f"Refusing to delete '{real}' — not a regular file.")
    os.unlink(real)
    return lst.st_size


def _extension(name: str) -> str:
    return name.rpartition(".")[2].lower() if "." in name else ""


def _protected_by_name(protected_paths: Collection[str]) -> dict[str, list[str]]:
    """Group the absolute *protected_paths* by file name, so only same-named ones are resolved."""
    by_name: dict[str, list[str]] = {}
    for path in protected_paths:
        if isinstance(path, str) and os.path.isabs(path):
            by_name.setdefault(os.path.basename(os.path.normpath(path)), []).append(path)
    return by_name


def _is_protected(path: Path, protected: dict[str, list[str]]) -> bool:
    """Return True if *path* is a protected path, as spelled or once both parents are resolved."""
    candidates = protected.get(path.name)
    if not candidates:
        return False
    normal = os.path.normpath(path)
    try:
        real = path.parent.resolve() / path.name
        return any(
            os.path.normpath(p) == normal or Path(p).parent.resolve() / path.name == real
            for p in candidates
        )
    except (OSError, RuntimeError):
        return True


def _sidecars_of(video: Path, protected: dict[str, list[str]]) -> list[Path]:
    """Return the sidecar files of *video* in its folder, skipping *protected* ones.

    Raises ``OSError`` when the folder is missing or cannot be listed.
    """
    folder = video.parent
    with os.scandir(folder.resolve()) as entries:
        regular = {
            e.name: e.is_file(follow_symlinks=False)
            for e in entries
            if not e.is_dir(follow_symlinks=False)
        }
    stem = video.stem
    owner_stems = {Path(n).stem for n in regular if _extension(n) not in _SIDECAR_EXTENSIONS}
    if stem in owner_stems:
        return []
    owner_stems.add(stem)
    sidecars: list[Path] = []
    for name, is_regular in regular.items():
        if not is_regular or not _is_sidecar(name, stem, owner_stems):
            continue
        candidate = folder / name
        if _is_protected(candidate, protected):
            continue
        sidecars.append(candidate)
    return sidecars


def _is_sidecar_tag(tag: str) -> bool:
    if tag.lower() in _SUBTITLE_FLAGS:
        return True
    match = _LANGUAGE_TAG.fullmatch(tag)
    return match is not None and match.group(1).lower() in _LANGUAGE_CODES


def _is_sidecar(name: str, stem: str, owner_stems: set[str]) -> bool:
    if not name.startswith(stem + "."):
        return False
    *tags, extension = name[len(stem) + 1 :].split(".")
    if extension.lower() not in _SIDECAR_EXTENSIONS:
        return False
    if not all(_is_sidecar_tag(tag) for tag in tags):
        return False
    owner = max((s for s in owner_stems if name.startswith(s + ".")), key=len)
    return owner == stem


def _is_folder_dressing(name: str) -> bool:
    return _extension(name) == "nfo" or _ARTWORK.fullmatch(name) is not None


def _remove_bare_folder(folder: Path, roots: list[Path]) -> bool:
    """Remove *folder* if it holds only artwork or ``.nfo`` files; return True if removed."""
    if any(folder == root or folder in root.parents for root in roots):
        return False
    matching = [root for root in roots if root in folder.parents]
    if not matching or _is_forbidden_root(folder):
        return False
    pinned = max(matching, key=lambda root: len(root.parts))
    try:
        root_dev = os.stat(pinned).st_dev
        fd = os.open(folder, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    except OSError:
        return False
    try:
        if os.fstat(fd).st_dev != root_dev:
            return False
        dressing: list[str] = []
        with os.scandir(fd) as entries:
            for entry in entries:
                entry_stat = entry.stat(follow_symlinks=False)
                if (
                    not _stat.S_ISREG(entry_stat.st_mode)
                    or entry_stat.st_dev != root_dev
                    or not _is_folder_dressing(entry.name)
                ):
                    return False
                dressing.append(entry.name)
        for name in dressing:
            os.unlink(name, dir_fd=fd)
    except OSError:
        return False
    finally:
        os.close(fd)
    try:
        # rmdir neither follows a final symlink nor removes a folder that
        # gained an entry since the scan.
        os.rmdir(folder)
    except OSError:
        return False
    return True
