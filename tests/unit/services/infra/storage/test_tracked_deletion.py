"""Tests for deleting an item's tracked files and the folders they leave empty."""

from __future__ import annotations

import os

import pytest

from mediaman.services.infra.storage import (
    DeletionRefused,
    delete_tracked_files,
    remove_empty_folders,
)


def _write(path, size=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _tracked(*paths):
    return [{"path": str(p), "size": p.stat().st_size if p.exists() else 0} for p in paths]


def _up_to(result, folder):
    return {path: str(folder) for path in result.removed}


def test_untracked_episode_and_its_season_folder_are_kept(tmp_path):
    root = tmp_path / "tv"
    season = root / "Show" / "Season 01"
    e1 = _write(season / "Show S01E01.mkv")
    e2 = _write(season / "Show S01E02.mkv")
    untracked = _write(season / "Show S01E03.mkv")

    result = delete_tracked_files(_tracked(e1, e2), allowed_roots=[str(root)])
    removed_folders = remove_empty_folders(
        result.removed, allowed_roots=[str(root)], boundaries=_up_to(result, root / "Show")
    )

    assert not e1.exists() and not e2.exists()
    assert untracked.exists()
    assert season.is_dir()
    assert removed_folders == []


def test_sidecars_of_a_tracked_film_go_but_a_stem_prefix_film_keeps_its_own(tmp_path):
    root = tmp_path / "movies"
    folder = root / "Alien"
    film = _write(folder / "Alien.mkv")
    sidecars = [
        _write(folder / "Alien.srt"),
        _write(folder / "Alien.en.srt"),
        _write(folder / "Alien.pt-BR.forced.ass"),
        _write(folder / "Alien.zh-Hans.srt"),
        _write(folder / "Alien.ger.sdh.srt"),
        _write(folder / "Alien.nfo"),
    ]
    kept = [
        _write(folder / "Alien.Resurrection.en.srt"),
        _write(folder / "Alien.Two.mkv"),
        _write(folder / "Alien.Two.srt"),
    ]

    result = delete_tracked_files(_tracked(film), allowed_roots=[str(root)])

    assert not film.exists()
    assert all(not s.exists() for s in sidecars)
    assert all(k.exists() for k in kept)
    assert sorted(result.removed) == sorted(str(p) for p in [film, *sidecars])


def test_a_sidecar_another_item_tracks_is_kept(tmp_path):
    root = tmp_path / "movies"
    film = _write(root / "Film" / "Film.mkv")
    shared = _write(root / "Film" / "Film.en.srt")

    delete_tracked_files(_tracked(film), allowed_roots=[str(root)], protected_paths={str(shared)})

    assert shared.exists()


def test_a_folder_left_with_only_artwork_goes_and_a_busy_parent_stays(tmp_path):
    root = tmp_path / "tv"
    show = root / "Show"
    season = show / "Season 02"
    episode = _write(season / "Show S02E01.mkv")
    _write(season / "poster.jpg")
    _write(season / "tvshow.nfo")
    kept = _write(show / "Season 01" / "Show S01E01.mkv")

    result = delete_tracked_files(_tracked(episode), allowed_roots=[str(root)])
    removed_folders = remove_empty_folders(
        result.removed, allowed_roots=[str(root)], boundaries=_up_to(result, show)
    )

    assert removed_folders == [str(season)]
    assert not season.exists()
    assert show.is_dir() and kept.exists()


def test_a_folder_with_non_artwork_left_is_kept(tmp_path):
    root = tmp_path / "movies"
    film = _write(root / "Film" / "Film.mkv")
    _write(root / "Film" / "poster.jpg")
    notes = _write(root / "Film" / "notes.txt")

    result = delete_tracked_files(_tracked(film), allowed_roots=[str(root)])

    boundaries = _up_to(result, root / "Film")
    assert (
        remove_empty_folders(result.removed, allowed_roots=[str(root)], boundaries=boundaries) == []
    )
    assert notes.exists() and (root / "Film" / "poster.jpg").exists()


def test_emptied_folders_walk_up_but_stop_below_the_root(tmp_path):
    root = tmp_path / "tv"
    episode = _write(root / "Show" / "Season 01" / "e01.mkv")
    _write(root / "Show" / "fanart.jpg")
    _write(root / "Show" / "season01-poster.png")

    result = delete_tracked_files(_tracked(episode), allowed_roots=[str(root)])
    removed_folders = remove_empty_folders(
        result.removed, allowed_roots=[str(root)], boundaries=_up_to(result, root / "Show")
    )

    assert removed_folders == [str(root / "Show" / "Season 01"), str(root / "Show")]
    assert root.is_dir()


def test_a_missing_tracked_file_is_reported_and_not_counted(tmp_path):
    root = tmp_path / "tv"
    present = _write(root / "Show" / "e01.mkv", size=100)
    missing = root / "Show" / "e02.mkv"

    result = delete_tracked_files(
        [{"path": str(present), "size": 100}, {"path": str(missing), "size": 5000}],
        allowed_roots=[str(root)],
    )

    assert result.removed == [str(present)]
    assert result.not_found == [str(missing)]
    assert result.bytes_freed == 100


def test_the_sidecars_of_an_already_removed_video_are_swept_when_named(tmp_path):
    root = tmp_path / "movies"
    video = root / "Film" / "Film.mkv"
    sidecar = _write(root / "Film" / "Film.en.srt", size=7)
    other = root / "Other" / "Other.mkv"
    other_sidecar = _write(root / "Other" / "Other.en.srt")

    result = delete_tracked_files(
        [{"path": str(video), "size": 100}, {"path": str(other), "size": 100}],
        allowed_roots=[str(root)],
        sweep_sidecars_of={str(video)},
    )

    assert not sidecar.exists()
    assert result.removed == [str(sidecar)]
    assert result.bytes_freed == 7
    assert other_sidecar.exists()


def test_a_tracked_file_outside_the_roots_is_refused_before_anything_is_removed(tmp_path):
    root = tmp_path / "tv"
    inside = _write(root / "Show" / "e01.mkv")
    outside = _write(tmp_path / "elsewhere" / "e02.mkv")

    with pytest.raises(DeletionRefused):
        delete_tracked_files(_tracked(inside, outside), allowed_roots=[str(root)])

    assert inside.exists() and outside.exists()


def test_a_symlinked_tracked_file_is_refused_and_its_target_kept(tmp_path):
    root = tmp_path / "movies"
    target = _write(root / "Real" / "Film.mkv")
    link = root / "Linked" / "Film.mkv"
    link.parent.mkdir(parents=True)
    os.symlink(target, link)

    with pytest.raises(DeletionRefused):
        delete_tracked_files(_tracked(link), allowed_roots=[str(root)])

    assert target.exists() and link.is_symlink()


def test_a_symlinked_sidecar_and_a_symlinked_folder_are_not_followed(tmp_path):
    root = tmp_path / "movies"
    elsewhere = _write(tmp_path / "elsewhere" / "subs.srt")
    film = _write(root / "Film" / "Film.mkv")
    os.symlink(elsewhere, root / "Film" / "Film.en.srt")
    linked_dir = root / "Film" / "extras"
    os.symlink(tmp_path / "elsewhere", linked_dir)

    result = delete_tracked_files(_tracked(film), allowed_roots=[str(root)])
    removed_folders = remove_empty_folders(
        result.removed, allowed_roots=[str(root)], boundaries=_up_to(result, root / "Film")
    )

    assert elsewhere.exists()
    assert (root / "Film" / "Film.en.srt").is_symlink()
    assert removed_folders == []
    assert linked_dir.is_symlink()


def test_a_folder_holding_a_symlinked_poster_is_kept(tmp_path):
    root = tmp_path / "movies"
    elsewhere = _write(tmp_path / "elsewhere" / "art.jpg")
    film = _write(root / "Film" / "Film.mkv")
    os.symlink(elsewhere, root / "Film" / "poster.jpg")

    result = delete_tracked_files(_tracked(film), allowed_roots=[str(root)])

    boundaries = _up_to(result, root / "Film")
    assert (
        remove_empty_folders(result.removed, allowed_roots=[str(root)], boundaries=boundaries) == []
    )
    assert (root / "Film" / "poster.jpg").is_symlink() and elsewhere.exists()


def test_a_folder_reached_through_a_symlinked_parent_is_not_removed(tmp_path):
    root = tmp_path / "movies"
    film = _write(root / "Real" / "Film.mkv")
    os.symlink(root / "Real", root / "Linked")

    result = delete_tracked_files(
        [{"path": str(root / "Linked" / "Film.mkv"), "size": 10}], allowed_roots=[str(root)]
    )

    assert not film.exists()
    boundaries = _up_to(result, root / "Real")
    assert (
        remove_empty_folders(result.removed, allowed_roots=[str(root)], boundaries=boundaries) == []
    )
    assert (root / "Real").is_dir()


def test_a_word_that_is_no_language_tag_does_not_make_a_sidecar(tmp_path):
    root = tmp_path / "movies"
    folder = root / "Alien"
    film = _write(folder / "Alien.mkv")
    other_film = _write(folder / "Alien.Two.mts")
    kept = [_write(folder / "Alien.Two.srt"), _write(folder / "Alien.the.nfo")]

    delete_tracked_files(_tracked(film), allowed_roots=[str(root)])

    assert not film.exists()
    assert other_film.exists()
    assert all(k.exists() for k in kept)


def test_a_sidecar_stays_while_another_file_with_its_stem_remains(tmp_path):
    root = tmp_path / "movies"
    film = _write(root / "Film" / "Film.mkv")
    other_cut = _write(root / "Film" / "Film.mp4")
    subtitles = _write(root / "Film" / "Film.en.srt")

    result = delete_tracked_files(_tracked(film), allowed_roots=[str(root)])

    assert result.removed == [str(film)]
    assert other_cut.exists() and subtitles.exists()


def test_a_protected_tracked_file_is_kept_and_reported(tmp_path):
    root = tmp_path / "movies"
    film = _write(root / "Film" / "Film.mkv")
    shared = _write(root / "Film" / "Shared.mkv")

    result = delete_tracked_files(
        _tracked(film, shared), allowed_roots=[str(root)], protected_paths={str(shared)}
    )

    assert shared.exists() and not film.exists()
    assert result.skipped_protected == [str(shared)]
    assert result.removed == [str(film)]


def test_a_protected_file_reached_through_a_symlinked_folder_is_kept(tmp_path):
    root = tmp_path / "movies"
    shared = _write(root / "Real" / "Shared.mkv")
    os.symlink(root / "Real", root / "Linked")

    result = delete_tracked_files(
        [{"path": str(root / "Linked" / "Shared.mkv"), "size": 10}],
        allowed_roots=[str(root)],
        protected_paths={str(shared)},
    )

    assert shared.exists()
    assert result.removed == []


def test_a_tracked_file_on_another_device_is_refused_before_anything_is_removed(
    tmp_path, monkeypatch
):
    root = tmp_path / "tv"
    first = _write(root / "Show" / "e01.mkv")
    foreign = _write(root / "Show" / "e02.mkv")
    real_lstat = os.lstat

    def lstat_with_foreign_device(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if os.fspath(path) == str(foreign):
            fields = list(st)
            fields[2] = st.st_dev + 1
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(os, "lstat", lstat_with_foreign_device)

    with pytest.raises(DeletionRefused):
        delete_tracked_files(_tracked(first, foreign), allowed_roots=[str(root)])

    assert first.exists() and foreign.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")
def test_a_file_that_cannot_be_unlinked_is_reported_and_the_rest_still_counted(tmp_path):
    root = tmp_path / "tv"
    removable = _write(root / "Show" / "e01.mkv", size=100)
    locked_folder = root / "Locked"
    stuck = _write(locked_folder / "e02.mkv")
    locked_folder.chmod(0o555)
    try:
        result = delete_tracked_files(_tracked(removable, stuck), allowed_roots=[str(root)])
    finally:
        locked_folder.chmod(0o755)

    assert result.removed == [str(removable)]
    assert result.bytes_freed == 100
    assert [path for path, _reason in result.failed] == [str(stuck)]
    assert stuck.exists()


def test_emptied_folders_are_not_removed_above_the_boundary(tmp_path):
    root = tmp_path / "data"
    film = _write(root / "movies" / "Film" / "Film.mkv")

    result = delete_tracked_files(_tracked(film), allowed_roots=[str(root)])
    removed_folders = remove_empty_folders(
        result.removed, allowed_roots=[str(root)], boundaries=_up_to(result, film.parent)
    )

    assert removed_folders == [str(film.parent)]
    assert (root / "movies").is_dir()


def test_a_configured_root_below_the_boundary_is_never_removed(tmp_path):
    outer = tmp_path / "x"
    movies = outer / "library" / "movies"
    film = _write(movies / "F" / "F.mkv")
    roots = [str(outer), str(movies)]

    result = delete_tracked_files(_tracked(film), allowed_roots=roots)
    removed_folders = remove_empty_folders(
        result.removed, allowed_roots=roots, boundaries=_up_to(result, outer / "library")
    )

    assert removed_folders == [str(movies / "F")]
    assert movies.is_dir()


@pytest.mark.parametrize("boundary", ["root", "outside", "missing"])
def test_a_boundary_that_is_not_below_a_root_removes_only_the_files_own_folder(tmp_path, boundary):
    root = tmp_path / "tv"
    episode = _write(root / "Show" / "Season 01" / "e01.mkv")
    _write(root / "Show" / "fanart.jpg")
    given = {"root": str(root), "outside": str(tmp_path / "elsewhere"), "missing": None}[boundary]
    boundaries = {} if given is None else {str(episode): given}

    result = delete_tracked_files(_tracked(episode), allowed_roots=[str(root)])
    removed_folders = remove_empty_folders(
        result.removed, allowed_roots=[str(root)], boundaries=boundaries
    )

    assert removed_folders == [str(root / "Show" / "Season 01")]
    assert (root / "Show" / "fanart.jpg").exists()
