"""Tests for another language of a title: subtitles Plex adds, and another copy with the audio."""

from __future__ import annotations

import defusedxml.ElementTree as ET
import pytest

from mediaman.services.media_meta.plex_watch import (
    PlexItem,
    PlexItemNotFound,
    PlexMedia,
    PlexWatch,
    SubtitleFound,
)
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.routes.search import download
from mediaman.web.routes.watch import languages_api, tracks_api

PLEX_URL = "http://plex-lan.example.test:32400"

# Episode 7 of season 1 (key 210) of a show, in Japanese and German, and a film in a library that
# is not scanned.
AUDIO = '<Stream id="11" streamType="2" languageCode="jpn" selected="1"/><Stream id="12" streamType="2" languageCode="ger"/>'
PART = {"201": "501"}


def _item(key: str) -> PlexItem:
    if key == "201":
        return PlexItem("episode", key, "Ep 7", "", None, 1500, "1", None, False, index=7,
                        parent_index=1, parent_rating_key="210", grandparent_rating_key="199",
                        grandparent_title="Show", media=PlexMedia(part_id=PART["201"], part_size="1300000000",
                                                                  resolution="1080"))  # fmt: skip
    if key == "301":
        return PlexItem("movie", key, "Elsewhere", "", 2020, 6000, "9", None, False)
    raise PlexItemNotFound("Item not found in Plex")


def _release(guid: str, languages: list[str], score: int, **more) -> dict:
    return {
        "guid": guid,
        "indexerId": 4,
        "indexer": "Nyaa",
        "languages": [{"id": 1, "name": n} for n in languages],
        "customFormatScore": score,
        "quality": {"quality": {"name": "Bluray-1080p"}},
        "size": 2_400_000_000,
        "age": 800,
        "rejected": False,
        "downloadAllowed": True,
        **more,
    }


class FakeSonarr:
    def __init__(self) -> None:
        self.releases = [
            _release("g-ja", ["Japanese"], 50),
            _release("g-en-ja", ["English", "Japanese"], 150),
            _release("g-en-rejected", ["English", "Japanese", "German"], 300, rejected=True),
            _release("g-en", ["English"], 100),
        ]
        self.searched: list[dict] = []
        self.grabbed: list[tuple[str, int]] = []
        self.queue: list[dict] = []

    def get_episodes(self, series_id):
        assert series_id == 77
        return [{"id": 9001, "seasonNumber": 1, "episodeNumber": 7}]

    def find_releases(self, **ids):
        self.searched.append(ids)
        return self.releases

    def grab_release(self, guid, indexer_id):
        self.grabbed.append((guid, indexer_id))

    def get_queue(self):
        return self.queue


class FakeManager:
    def __init__(self) -> None:
        self.added: list[tuple[str, str]] = []

    def add_subtitle(self, rating_key, key):
        self.added.append((rating_key, key))
        return "40"


@pytest.fixture
def sonarr():
    return FakeSonarr()


@pytest.fixture
def manager():
    return FakeManager()


@pytest.fixture
def app(app_factory, conn, monkeypatch, sonarr, manager):
    for key, value in (("plex_url", PLEX_URL), ("plex_token", "tok"), ("plex_libraries", '["1"]')):
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at)"
            " VALUES (?, ?, 0, datetime('now'))",
            (key, value),
        )
    conn.execute(
        "INSERT INTO media_items (id, title, media_type, season_number, plex_library_id,"
        " plex_rating_key, sonarr_id, added_at, file_path, file_size_bytes)"
        " VALUES ('210', 'Show', 'anime_season', 1, 1, '210', 77, datetime('now'), '/m', 1)"
    )
    conn.commit()
    for limiter in ("_LIMITER", "_COPY_SEARCH_LIMITER"):
        monkeypatch.setattr(languages_api, limiter, ActionRateLimiter(60, 60))
    monkeypatch.setattr(tracks_api, "_LIMITER", ActionRateLimiter(60, 60))
    monkeypatch.setattr(download, "_DOWNLOAD_ADMIN_LIMITER", ActionRateLimiter(60, 60))
    monkeypatch.setattr(PlexWatch, "get_item", lambda self, key, marks=False: _item(key))
    monkeypatch.setattr(
        PlexWatch,
        "_metadata",
        lambda self, key, query="": (
            None,
            ET.fromstring(
                f"<Video><Media><Part>{AUDIO if key == '201' else ''}</Part></Media></Video>"
            ),
        ),
    )
    monkeypatch.setattr(
        PlexWatch,
        "search_subtitles",
        lambda self, key, language: [
            SubtitleFound(
                f"/sub/{language}/1", "Show.S01E07.srt", "OpenSubtitles", 9.0, False, False
            )
        ],
    )
    monkeypatch.setattr(languages_api, "build_sonarr_from_db", lambda conn, secret: sonarr)
    monkeypatch.setattr(languages_api, "get_stream_manager", lambda: manager)
    return app_factory(languages_api.router, conn=conn)


@pytest.fixture
def alice(app, conn, authed_client):
    return authed_client(app, conn, username="alice")


@pytest.fixture
def bob(app, conn, authed_client):
    return authed_client(app, conn, username="bob")


def _search(client, language="en", scope="episode"):
    return client.get(f"/api/watch/languages/201/audio?language={language}&scope={scope}")


def test_copies_offered_have_the_language_and_would_be_taken_best_first(alice, sonarr):
    resp = _search(alice)

    assert resp.status_code == 200, resp.text
    found = resp.json()["found"]
    # Japanese alone lacks it; the one Sonarr rejects would never take the file's place.
    assert [c["languages"] for c in found] == [["en", "ja"], ["en"]]
    assert [c["loses"] for c in found] == [["de"], ["ja", "de"]]
    assert sonarr.searched == [{"episodeId": 9001}]


def test_a_whole_season_is_searched_by_its_series_and_number(alice, sonarr):
    assert _search(alice, scope="season").status_code == 200
    assert sonarr.searched == [{"seriesId": 77, "seasonNumber": 1}]


def test_a_copy_is_downloaded_only_from_what_that_person_s_own_search_found(alice, bob, sonarr):
    _search(alice)
    body = {"language": "en", "scope": "episode", "index": 0}

    refused = bob.post("/api/watch/languages/201/audio", json=body)
    assert refused.status_code == 409
    assert sonarr.grabbed == []

    resp = alice.post("/api/watch/languages/201/audio", json=body)
    assert resp.status_code == 200, resp.text
    assert sonarr.grabbed == [("g-en-ja", 4)]


def test_no_search_or_copy_is_had_for_a_title_the_person_cannot_watch(alice, sonarr, manager):
    assert alice.get("/api/watch/languages/301/audio?language=en&scope=movie").status_code == 404
    post = {"language": "en", "scope": "movie", "index": 0}
    assert alice.post("/api/watch/languages/301/audio", json=post).status_code == 404
    sub = {"language": "en", "index": 0}
    assert alice.post("/api/watch/languages/301/subtitles", json=sub).status_code == 404
    assert sonarr.searched == sonarr.grabbed == manager.added == []


def test_a_copy_asked_for_shows_its_progress_then_says_when_it_came_without_the_language(
    alice, bob, sonarr
):
    _search(alice)
    alice.post(
        "/api/watch/languages/201/audio", json={"language": "en", "scope": "episode", "index": 0}
    )
    sonarr.queue = [
        {"episodeId": 9001, "size": 1000, "sizeleft": 660, "trackedDownloadState": "downloading"}
    ]

    # Anyone watching the episode sees it on its way.
    copies = bob.get("/api/watch/languages/201").json()["copies"]
    assert [(c["language"], c["status"], c["progress"]) for c in copies] == [
        ("en", "downloading", 34)
    ]

    # In Plex as a new file, still Japanese and German only.
    sonarr.queue = []
    PART["201"] = "502"
    try:
        copies = alice.get("/api/watch/languages/201").json()["copies"]
    finally:
        PART["201"] = "501"
    assert [(c["language"], c["status"]) for c in copies] == [("en", "lacks")]


def test_subtitles_added_are_only_those_the_person_s_search_found(alice, bob, manager):
    assert alice.get("/api/watch/languages/201/subtitles?language=pt").status_code == 200

    assert (
        bob.post(
            "/api/watch/languages/201/subtitles", json={"language": "pt", "index": 0}
        ).status_code
        == 409
    )
    resp = alice.post("/api/watch/languages/201/subtitles", json={"language": "pt", "index": 0})

    assert resp.json()["subtitle"] == "40"
    assert manager.added == [("201", "/sub/pt/1")]
