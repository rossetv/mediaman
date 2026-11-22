"""Tests for the delete-root allowlist checks."""

from __future__ import annotations

import os

import pytest

from mediaman.services.infra import DeletionRefused, delete_path


@pytest.mark.parametrize("root", ["/Users", "/Volumes"])
def test_mac_home_and_drive_folders_are_refused_as_roots(root):
    with pytest.raises(DeletionRefused, match="system / mount-root"):
        delete_path(f"{root}/somefile", allowed_roots=[root])


@pytest.mark.parametrize("root", ["/usr", "/bin", "/private/tmp"])
def test_another_case_of_a_denied_folder_is_refused(root):
    other_case = root.upper()
    try:
        same_folder = os.path.samefile(root, other_case)
    except OSError:
        same_folder = False
    if not same_folder:
        pytest.skip(f"{other_case} is not {root} on this filesystem")
    with pytest.raises(DeletionRefused, match="system / mount-root"):
        delete_path(f"{other_case}/somefile", allowed_roots=[other_case])


def test_a_folder_under_a_denied_folder_is_accepted(tmp_path):
    target = tmp_path / "movie.mkv"
    target.write_text("x")

    delete_path(str(target), allowed_roots=[str(tmp_path)])

    assert not target.exists()
