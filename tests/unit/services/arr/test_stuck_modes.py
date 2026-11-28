"""Why a Radarr or Sonarr queue record is stuck, read from what the service says verbatim."""

from __future__ import annotations

from datetime import timedelta

import pytest

from mediaman.services.arr import stuck_modes as m
from mediaman.services.downloads.download_format import attention_reason

_HELD = ("completed", "importPending", "warning")


def _mode(message: str, held: tuple[str, str, str] = _HELD) -> str:
    """The mode of a record held for *message*, read as the fetcher reads it."""
    release = "Adventure.Time.2010.S02E03.Loyalty.to.the.King.1080p.EN-GR.BluRay.x264-Smad"
    record = {"title": release, "statusMessages": [{"title": release, "messages": [message]}]}
    return m.classify(*held, attention_reason(record))


@pytest.mark.parametrize(
    ("message", "mode"),
    [
        (
            "No files found are eligible for import in /downloads/completed/comptv/"
            "Adventure.Time.2010.S02E03.Loyalty.to.the.King.1080p.EN-GR.BluRay.x264-Smad",
            m.NO_FILES,
        ),
        ("Sample", m.SAMPLE),
        ("Unable to determine if file is a sample", m.SAMPLE),
        (
            "Single episode file contains all episodes in seasons. Review file name or manually"
            " import",
            m.MULTI_SEASON,
        ),
        (
            "Found matching series via grab history, but release was matched to series by ID."
            " Automatic import is not possible. See the FAQ for details.",
            m.WRONG_MATCH,
        ),
        (
            "Found matching movie via grab history, but release was matched to movie by ID."
            " Manual Import required.",
            m.WRONG_MATCH,
        ),
        ("Episode wasn't in the release", m.OTHER),
        (
            "One or more episodes expected in this release were not imported or missing from the"
            " release",
            m.OTHER,
        ),
        ("Not an upgrade for existing episode file(s)", m.OTHER),
        ("Not enough free space", m.SETUP),
        (
            "Import failed, path does not exist or is not accessible by Sonarr:"
            " /downloads/completed/comptv/x",
            m.SETUP,
        ),
        ("Episode has a TBA title and recently aired", m.WAITING),
    ],
)
def test_each_message_sonarr_or_radarr_gives_is_read_as_its_mode(message, mode):
    assert _mode(message) == mode


@pytest.mark.parametrize("state", ["failed", "failedPending"])
def test_a_download_that_failed_is_failed_whatever_it_says(state):
    assert _mode("Download failed", ("failed", state, "error")) == m.FAILED


def test_a_stalled_torrent_is_stalled_and_a_downloading_one_with_a_warning_is_left_alone():
    downloading = ("warning", "downloading", "warning")
    assert _mode("The download is stalled with no connections", downloading) == m.STALLED
    assert _mode("Something else", downloading) == m.HEALTHY


def test_a_paused_download_and_one_no_client_holds_are_read_apart():
    assert m.classify("paused", "downloading", "ok", "") == m.PAUSED
    assert m.classify("downloadClientUnavailable", "downloading", "warning", "") == m.WAITING
    assert m.classify("downloading", "downloading", "ok", "") == m.HEALTHY


def test_each_next_copy_is_left_longer_and_then_a_day():
    waits = [m.wait_for(n) for n in range(6)]
    hours = [timedelta(hours=h) for h in (1, 3, 12, 24, 24, 24)]
    assert waits == hours
    assert m.wait_for(0, m.STALLED) == m.UNFINISHED_FOR
    assert m.wait_for(0, m.PAUSED) == timedelta(hours=1)
