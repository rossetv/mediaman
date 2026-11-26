"""The reason shown for a download Sonarr or Radarr holds is the line that says what keeps it."""

from __future__ import annotations

from mediaman.services.downloads.download_format import attention_reason


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
