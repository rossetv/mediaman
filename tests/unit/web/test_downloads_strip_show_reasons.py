"""A show whose stuck episodes say different things is judged by all of them, as the Watch
page judges it: one setup problem among them and another copy is not looked for."""

from __future__ import annotations

from tests.unit.web.test_downloads_strip import _get, services, viewer  # noqa: F401


def _stuck_episode(n: int, why: str) -> dict[str, object]:
    return {
        "series": {"id": 5, "title": "Salt Flats", "tmdbId": 5},
        "episode": {"seasonNumber": 1, "episodeNumber": n, "title": f"E{n}"},
        "title": f"Salt.Flats.S01E0{n}",
        "status": "completed",
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": "importPending",
        "statusMessages": [{"title": f"Salt.Flats.S01E0{n}", "messages": [why]}],
        "size": 1000,
        "sizeleft": 0,
        "downloadId": f"d{n}",
    }


def test_a_show_with_a_setup_problem_in_a_later_episode_is_not_said_to_be_tried_again(
    services,  # noqa: F811
    viewer,  # noqa: F811
):
    radarr, sonarr, _ = services
    radarr.get_queue.return_value = []
    radarr.get_movies.return_value = []
    sonarr.get_queue.return_value = [
        _stuck_episode(1, "Not a sample"),
        _stuck_episode(2, "Not enough free space on the disk"),
    ]
    items = _get(viewer).json()["items"]
    (item,) = items
    assert (item["state"], item["retrying"]) == ("needs_attention", False)
