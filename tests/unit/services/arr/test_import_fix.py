"""Mending a stuck download where it lies: renaming its video files inside its own folder alone,
and importing by hand only what the service matched beyond doubt."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mediaman.db import init_db
from mediaman.services.arr import import_fix
from tests.helpers.factories import insert_settings

_MKV = b"\x1a\x45\xdf\xa3" + b"\0" * 60
_MP4 = b"\0\0\0\x20ftypisom" + b"\0" * 60
_AVI = b"RIFF\0\0\0\0AVI LIST" + b"\0" * 60
_RELEASE = "Adventure.Time.2010.S02E03.Loyalty.to.the.King.1080p.EN-GR.BluRay.x264-Smad"


@pytest.fixture
def root(tmp_path) -> Path:
    folder = tmp_path / "completed"
    (folder / "comptv" / _RELEASE).mkdir(parents=True)
    return folder


def _release(root: Path) -> Path:
    return root / "comptv" / _RELEASE


def _found(root: Path, output_path: str, release: str = _RELEASE) -> Path | None:
    return import_fix.release_folder(root, output_path, release)


@pytest.mark.parametrize(
    ("head", "extension"),
    [(_MKV, ".mkv"), (_MP4, ".mp4"), (_AVI, ".avi"), (b"<?xml version", ""), (b"", "")],
)
def test_a_files_first_bytes_say_which_video_it_is(head, extension):
    assert import_fix.video_extension(head) == extension


def test_an_obfuscated_video_is_given_its_extension_and_nothing_else_is_touched(root):
    folder = _release(root)
    (folder / "a8f3c2e9b1").write_bytes(_MKV)
    (folder / "b1.nfo").write_bytes(b"release notes")
    (folder / "clip.mp4").write_bytes(_MKV)

    renamed = import_fix.rename_videos(_found(root, str(folder)))

    assert renamed == ["a8f3c2e9b1.mkv"]
    assert sorted(p.name for p in folder.iterdir()) == ["a8f3c2e9b1.mkv", "b1.nfo", "clip.mp4"]
    assert (folder / "a8f3c2e9b1.mkv").read_bytes() == _MKV


def test_a_file_whose_new_name_is_taken_is_left_as_it_is(root):
    folder = _release(root)
    (folder / "video").write_bytes(_MKV)
    (folder / "video.mkv").write_bytes(b"another file")

    assert import_fix.rename_videos(folder) == []
    assert (folder / "video.mkv").read_bytes() == b"another file"
    assert (folder / "video").read_bytes() == _MKV


def test_a_symlinked_file_or_folder_is_never_followed(root, tmp_path):
    outside = tmp_path / "library"
    outside.mkdir()
    (outside / "film").write_bytes(_MKV)
    folder = _release(root)
    os.symlink(outside / "film", folder / "link")
    os.symlink(outside, folder / "linked-dir")

    assert import_fix.rename_videos(folder) == []
    assert sorted(p.name for p in outside.iterdir()) == ["film"]


@pytest.mark.parametrize(
    "output_path",
    [
        "/elsewhere/comptv/" + _RELEASE,
        "{root}",
        "{root}/comptv",
        "{root}/comptv/../comptv/" + _RELEASE,
        "{root}/comptv/" + _RELEASE + "-missing",
        "comptv/" + _RELEASE,
    ],
    ids=["outside", "the-root", "a-shared-folder", "dot-dot", "missing", "relative"],
)
def test_a_folder_that_is_not_the_releases_own_inside_the_root_is_refused(root, output_path):
    assert _found(root, output_path.format(root=root)) is None


def test_a_release_folder_that_is_a_symlink_out_of_the_root_is_refused(root, tmp_path):
    outside = tmp_path / "library" / _RELEASE
    outside.mkdir(parents=True)
    os.symlink(outside, root / "comptv" / (_RELEASE + ".#1"))

    assert _found(root, f"{root}/comptv/{_RELEASE}.#1") is None
    assert _found(root, str(_release(root))) == _release(root).resolve()


@pytest.mark.parametrize(
    ("named", "used"),
    [
        (None, False),
        ("", False),
        ("nowhere", False),
        ("shut", False),
        ("relative", False),
        (".", True),
    ],
)
def test_the_downloads_folder_is_used_only_while_it_is_set_there_and_writable(
    tmp_path, named, used
):
    (tmp_path / "shut").mkdir(mode=0o500)
    conn = init_db(str(tmp_path / "mediaman.db"))
    if named is not None:
        value = named if named in ("", "relative") else str((tmp_path / named).resolve())
        insert_settings(conn, import_fix_folder=value)
    found = import_fix.downloads_folder(conn)
    conn.close()
    assert (found is not None) == used


def _file(series: int = 5, episodes: tuple[int, ...] = (501,), **more) -> dict:
    return {
        "path": f"/downloads/completed/comptv/x/{series}-{len(episodes)}.mkv",
        "folderName": "x",
        "series": {"id": series},
        "episodes": [{"id": e} for e in episodes],
        "quality": {"quality": {"id": 7}},
        "languages": [{"id": 1}],
        "releaseGroup": "Smad",
        "downloadId": "nzb-1",
        "releaseType": "singleEpisode",
        "rejections": [],
        **more,
    }


def test_files_matched_to_the_show_and_episodes_waited_for_are_imported_by_hand():
    files = import_fix.manual_import_files(
        "sonarr", [_file(), _file(episodes=(502,))], 5, {501, 502}
    )
    assert [(f["seriesId"], f["episodeIds"]) for f in files] == [(5, [501]), (5, [502])]


@pytest.mark.parametrize(
    "listed",
    [
        [],
        [_file(series=6)],
        [_file(episodes=(999,))],
        [_file(episodes=())],
        [_file(rejections=[{"reason": "Sample", "type": "permanent"}])],
        [_file(), _file()],
        [_file(quality=None)],
    ],
    ids=[
        "none",
        "other-show",
        "unwaited-episode",
        "no-episode",
        "rejected",
        "same-episode",
        "no-quality",
    ],
)
def test_nothing_is_imported_by_hand_unless_every_file_is_beyond_doubt(listed):
    assert import_fix.manual_import_files("sonarr", listed, 5, {501, 502}) == []


def test_a_film_is_imported_by_hand_only_from_one_file_of_that_film():
    film = {**_file(), "movie": {"id": 9}}
    assert import_fix.manual_import_files("radarr", [film], 9, ())[0]["movieId"] == 9
    assert import_fix.manual_import_files("radarr", [film, film], 9, ()) == []
    assert import_fix.manual_import_files("radarr", [film], 10, ()) == []
