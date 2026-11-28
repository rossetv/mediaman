"""Mending a stuck download where it lies, before another copy is looked for.

A release whose video file came with no extension at all is given the one its first bytes say,
inside its own folder alone, and Radarr or Sonarr is asked to import that folder again. A release
the service matched by id alone is imported by hand, only when the service's own reading of every
file names that title and episodes it waits for; nothing is ever guessed.

The downloads folder is only touched when an admin names it, as the path the service reports it
at, and mediaman can write to it. Library files are never touched, nor anything outside the
stuck download's own folder, nor a file that would take another's name.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Collection
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlencode

from mediaman.services.infra import get_string_setting

logger = logging.getLogger(__name__)

FOLDER_SETTING = "import_fix_folder"

# Each release folder is read this deep and this far, so a huge one costs nothing.
_MAX_DEPTH = 3
_MAX_FILES = 200


def downloads_folder(conn: sqlite3.Connection) -> Path | None:
    """The downloads folder an admin named, while mediaman can write to it; else None."""
    try:
        named = get_string_setting(conn, FOLDER_SETTING, default="").strip()
    except sqlite3.DatabaseError:
        return None
    if not named or not named.startswith("/"):
        return None
    folder = Path(named)
    if not folder.is_dir() or not os.access(folder, os.W_OK | os.X_OK):
        return None
    return folder


def video_extension(head: bytes) -> str:
    """The extension of the video file whose first bytes are *head*, "" when it is none."""
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".mkv"
    if head[4:8] == b"ftyp":
        return ".mp4"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return ".avi"
    return ""


def _plain(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())


def release_folder(root: Path, output_path: str, release: str) -> Path | None:
    """The folder of the download *release* the service reports at *output_path*, when it lies
    strictly inside *root*, which is named as the service names it, and is named for the release,
    so a folder other downloads share is never read; else None."""
    if not output_path.startswith("/") or not _plain(release):
        return None
    reported = PurePosixPath(output_path)
    try:
        inner = reported.relative_to(PurePosixPath(root))
    except ValueError:
        return None
    if not inner.parts or ".." in inner.parts:
        return None
    if not _plain(inner.parts[-1]).startswith(_plain(release)):
        return None
    try:
        real_root = root.resolve(strict=True)
        folder = (root / inner).resolve(strict=True)
    except OSError:
        return None
    if folder == real_root or not folder.is_relative_to(real_root) or not folder.is_dir():
        return None
    # A release name that links to another folder, such as the one downloads share, is not it.
    if not _plain(folder.name).startswith(_plain(release)):
        return None
    return folder


def _files(folder: Path) -> list[Path]:
    """The regular files in *folder* and its folders, symlinks left out."""
    found: list[Path] = []
    for here, dirs, names in os.walk(folder, followlinks=False):
        depth = len(Path(here).relative_to(folder).parts)
        if depth >= _MAX_DEPTH:
            dirs.clear()
        dirs[:] = [d for d in dirs if not (Path(here) / d).is_symlink()]
        for name in names:
            path = Path(here) / name
            if not path.is_symlink() and path.is_file():
                found.append(path)
            if len(found) >= _MAX_FILES:
                return found
    return found


def _rename(path: Path, target: Path) -> bool:
    """Give *path* the name *target*, never over a file there: a hard link fails if one is."""
    try:
        os.link(path, target, follow_symlinks=False)
    except FileExistsError:
        return False
    except OSError:
        # A filesystem with no hard links: the name is taken first, so nothing else can be there
        # when the file is moved onto it.
        try:
            os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            return False
        try:
            os.rename(path, target)
        except OSError:
            os.unlink(target)
            raise
        return True
    os.unlink(path)
    return True


def rename_videos(folder: Path) -> list[str]:
    """Give each video file in *folder* with no extension at all the one its first bytes say it
    is; the new names, of those renamed. A file with any extension is left as it is."""
    renamed: list[str] = []
    for path in _files(folder):
        if path.suffix:
            continue
        try:
            with path.open("rb") as handle:
                extension = video_extension(handle.read(12))
            if not extension:
                continue
            target = path.with_name(path.name + extension)
            if not path.resolve().is_relative_to(folder):
                continue
            if _rename(path, target):
                renamed.append(target.name)
        except OSError:
            logger.warning("Could not rename %s", path, exc_info=True)
    return renamed


def scan_command(service: str, output_path: str, download_id: str) -> dict[str, Any]:
    """The command that has *service* import the download at *output_path* again."""
    name = "DownloadedMoviesScan" if service == "radarr" else "DownloadedEpisodesScan"
    command: dict[str, Any] = {"name": name, "path": output_path, "importMode": "auto"}
    if download_id:
        command["downloadClientId"] = download_id
    return command


def _ids(found: object) -> list[int]:
    if not isinstance(found, list):
        return []
    return [i for e in found if isinstance(e, dict) and isinstance(i := e.get("id"), int)]


def manual_import_files(
    service: str, listed: list[dict[str, Any]], arr_id: int, episodes: Collection[int]
) -> list[dict[str, Any]]:
    """The files to import by hand of the download's files *listed* by the service, as
    ``/api/v3/manualimport`` gives them; none unless every one passes: the service matched it to
    the film or show *arr_id*, gives no rejection of it and gives its quality; of a show, it names
    episodes, all of them among the queued *episodes*, and no episode another file names; of a
    film, it is the download's one file."""
    if not listed or (service == "radarr" and len(listed) != 1):
        return []
    files: list[dict[str, Any]] = []
    taken: set[int] = set()
    for entry in listed:
        if entry.get("rejections") or not isinstance(path := entry.get("path"), str):
            return []
        owner = entry.get("movie" if service == "radarr" else "series")
        if not isinstance(owner, dict) or owner.get("id") != arr_id:
            return []
        file: dict[str, Any] = {
            "path": path,
            "folderName": entry.get("folderName") or "",
            "quality": entry.get("quality"),
            "languages": entry.get("languages") or [],
            "releaseGroup": entry.get("releaseGroup") or "",
            "indexerFlags": entry.get("indexerFlags") or 0,
            "downloadId": entry.get("downloadId") or "",
        }
        if service == "radarr":
            file["movieId"] = arr_id
        else:
            ids = _ids(entry.get("episodes"))
            if not ids or not set(ids) <= set(episodes) or taken & set(ids):
                return []
            taken |= set(ids)
            file.update(seriesId=arr_id, episodeIds=ids, releaseType=entry.get("releaseType"))
        if file["quality"] is None:
            return []
        files.append(file)
    return files


def manual_import_query(service: str, download_id: str, arr_id: int) -> str:
    """The path that lists the files of *download_id* as the service would import them."""
    owner = "movieId" if service == "radarr" else "seriesId"
    query = urlencode({"downloadId": download_id, owner: arr_id, "filterExistingFiles": "true"})
    return f"/api/v3/manualimport?{query}"
