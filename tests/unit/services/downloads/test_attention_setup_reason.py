"""The reason shown for a download Sonarr or Radarr holds is the line that says what keeps it."""

from __future__ import annotations

import pytest

from mediaman.services.downloads.download_format import attention_reason
from mediaman.services.downloads.download_format._types import attention_hint


def _held(*messages: dict[str, object]) -> dict[str, object]:
    return {"title": "Alone.S13.1080p", "statusMessages": list(messages), "errorMessage": ""}


def test_a_setup_fault_after_a_message_saying_only_that_files_failed_is_the_reason():
    record = _held(
        {
            "title": "One or more episodes expected in this release were not imported",
            "messages": [],
        },
        {"title": "Alone.S13E01.mkv", "messages": ["Not a sample"]},
        {"title": "Alone.S13E02.mkv", "messages": ["Not enough free space"]},
    )

    assert attention_reason(record) == "Not enough free space"


def test_a_long_setup_fault_keeps_the_words_that_name_it():
    path = "/downloads/" + "deep/" * 40
    record = _held({"title": "x", "messages": [f"[{path}] is not a valid local path. Fix it."]})

    reason = attention_reason(record)

    assert len(reason) <= 160
    assert "not a valid local path" in reason


@pytest.mark.parametrize(
    ("reason", "fix"),
    [
        ("Import failed, path does not exist or is not accessible by Sonarr: /x", "Remote Path"),
        ("Access to the path '/tv/Alone' is denied", "access to the download folder"),
        ("Root folder missing: /tv", "root folder"),
        ("Not enough free space", "disk space"),
        ("Can't reach the download client", "Download Clients"),
    ],
)
def test_a_fault_of_the_services_own_setup_says_what_to_put_right_there(reason, fix):
    assert fix.lower() in attention_hint("Sonarr", reason).lower()


def test_a_download_whose_client_is_unreachable_says_so_where_the_service_says_nothing():
    assert (
        attention_reason({"status": "downloadClientUnavailable"})
        == "Can't reach the download client"
    )
