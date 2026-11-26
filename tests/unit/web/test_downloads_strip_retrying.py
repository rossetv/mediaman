"""The Watch page's row says of a download that needs attention whether mediaman will look
for another copy unasked, to an admin and a viewer alike."""

from __future__ import annotations

import pytest

from tests.unit.web.test_downloads_strip import _get, _movie, services, viewer  # noqa: F401

_WHY = "No files found are eligible for import"


@pytest.fixture
def stuck(services):  # noqa: F811
    radarr = services[0]
    film = _movie(11, "Ember Coast")
    radarr.get_movies.return_value = [film]
    radarr.get_queue.return_value = [
        {
            "movie": film,
            "title": "Ember Coast",
            "status": "completed",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importPending",
            "statusMessages": [{"title": "Ember.Coast", "messages": [_WHY]}],
            "size": 1000,
            "sizeleft": 0,
            "downloadId": "11",
        }
    ]


def _item(client):
    (item,) = _get(client).json()["items"]
    return item


def test_a_viewer_is_told_it_will_be_tried_again_but_not_what_radarr_says(stuck, viewer, conn):  # noqa: F811
    item = _item(viewer)
    assert (item["state"], item["retrying"]) == ("needs_attention", True)
    assert (item["attention"], item["attention_hint"]) == ("", "")


def test_a_download_whose_tries_are_spent_is_not_said_to_be_tried_again(stuck, viewer, conn):  # noqa: F811
    conn.execute(
        "INSERT INTO arr_import_retries (key, tries, stuck_since) VALUES ('radarr:11', 99, 1)"
    )
    conn.commit()
    assert _item(viewer)["retrying"] is False
