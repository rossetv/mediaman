"""Tests for another language of a title: subtitles Plex adds, and another copy with the audio."""

from __future__ import annotations

import threading

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
# Episodes 7 and 8 of season 1, by the file each has in Plex.
PART = {"201": "501", "202": "502"}


def _item(key: str) -> PlexItem:
    if key in PART:
        media = PlexMedia(part_id=PART[key], part_size="1300000000", resolution="1080")
        return PlexItem("episode", key, "Ep", "", None, 1500, "1", None, False,
                        index=int(key) - 194, parent_index=1, parent_rating_key="210",
                        grandparent_rating_key="199", grandparent_title="Show", media=media)  # fmt: skip
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
        return [
            {"id": 9001, "seasonNumber": 1, "episodeNumber": 7},
            {"id": 9002, "seasonNumber": 1, "episodeNumber": 8},
        ]

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
                f"<Video><Media><Part>{AUDIO if key in PART else ''}</Part></Media></Video>"
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
    monkeypatch.setattr(PlexWatch, "episode_parts", lambda self, season: dict(PART))
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
    PART["201"] = "601"
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


def test_settled_and_long_asked_copies_are_forgotten(conn, authed_client, app):
    from datetime import UTC, datetime, timedelta

    from mediaman.web.repository import language_copies

    authed_client(app, conn, username="carol")
    now = datetime(2026, 11, 28, 12, tzinfo=UTC)
    rows = [("asked", 1, None), ("asked", 40, None), ("lacks", 20, 15), ("ready", 3, 2)]
    for state, asked_days, done_days in rows:
        conn.execute(
            "INSERT INTO language_copies (username, rating_key, scope, language, service, arr_id,"
            " release_languages, quality, asked_at, state, done_at)"
            " VALUES ('carol', '1', 'movie', 'en', 'radarr', 1, '[]', '', ?, ?, ?)",
            (
                (now - timedelta(days=asked_days)).isoformat(),
                state,
                None if done_days is None else (now - timedelta(days=done_days)).isoformat(),
            ),
        )
    conn.commit()

    assert language_copies.prune(conn, now) == 2

    left = conn.execute("SELECT state FROM language_copies ORDER BY id").fetchall()
    assert [r[0] for r in left] == ["asked", "ready"]


def test_a_season_copy_that_came_without_the_language_says_so_on_each_episode(alice, sonarr):
    _search(alice, scope="season")
    alice.post(
        "/api/watch/languages/201/audio", json={"language": "en", "scope": "season", "index": 0}
    )
    assert [c["status"] for c in alice.get("/api/watch/languages/202").json()["copies"]] == [
        "waiting"
    ]

    PART["202"] = "602"
    try:
        copies = alice.get("/api/watch/languages/202").json()["copies"]
    finally:
        PART["202"] = "502"

    assert [(c["scope"], c["status"]) for c in copies] == [("season", "lacks")]


def test_the_same_copy_is_not_downloaded_twice(alice, sonarr):
    _search(alice)
    body = {"language": "en", "scope": "episode", "index": 0}
    assert alice.post("/api/watch/languages/201/audio", json=body).status_code == 200
    _search(alice)

    again = alice.post("/api/watch/languages/201/audio", json=body)

    assert again.status_code == 409
    assert again.json()["error"] == "already_asked"
    assert sonarr.grabbed == [("g-en-ja", 4)]


def test_a_copy_on_its_way_says_how_long_it_has_left(alice, sonarr):
    _search(alice)
    alice.post(
        "/api/watch/languages/201/audio", json={"language": "en", "scope": "episode", "index": 0}
    )
    sonarr.queue = [
        {"episodeId": 9001, "size": 10, "sizeleft": 5, "timeleft": "00:12:20"},
        {"episodeId": 9001, "size": 10, "sizeleft": 5, "timeleft": "1.01:00:00"},
    ]

    (copy,) = alice.get("/api/watch/languages/201").json()["copies"]

    assert copy["minutes_left"] == 1500


@pytest.mark.parametrize(
    ("limiter", "module", "ask"),
    [
        ("_LIMITER", languages_api, lambda c: c.get("/api/watch/languages/201")),
        (
            "_LIMITER",
            languages_api,
            lambda c: c.get("/api/watch/languages/201/subtitles?language=de"),
        ),
        ("_COPY_SEARCH_LIMITER", languages_api, lambda c: _search(c)),
        (
            "_DOWNLOAD_ADMIN_LIMITER",
            download,
            lambda c: c.post(
                "/api/watch/languages/201/audio",
                json={"language": "en", "scope": "episode", "index": 0},
            ),
        ),
    ],
)
def test_each_way_is_limited_per_person(alice, monkeypatch, limiter, module, ask):
    monkeypatch.setattr(module, limiter, ActionRateLimiter(1, 60))
    ask(alice)
    assert ask(alice).status_code == 429


@pytest.mark.parametrize(
    "path",
    [
        "/api/watch/languages/201/subtitles?language=zz",
        "/api/watch/languages/201/subtitles?language=por",
        "/api/watch/languages/201/audio?language=zz&scope=episode",
        "/api/watch/languages/201/audio?language=en&scope=everything",
    ],
)
def test_an_unknown_language_or_scope_is_refused_before_plex_or_sonarr_is_asked(
    alice, monkeypatch, sonarr, path
):
    asked = []
    monkeypatch.setattr(PlexWatch, "get_item", lambda self, key, marks=False: asked.append(key))

    assert alice.get(path).status_code == 422
    assert asked == sonarr.searched == []


def test_a_film_has_no_episode_or_season_to_search(alice, monkeypatch, sonarr):
    film = PlexItem("movie", "401", "F", "", 2020, 60, "1", None, False)
    monkeypatch.setattr(PlexWatch, "get_item", lambda self, key, marks=False: film)
    assert alice.get("/api/watch/languages/401/audio?language=en&scope=season").status_code == 422
    assert sonarr.searched == []


def test_a_person_has_at_most_so_many_subtitle_adds_under_way(alice, monkeypatch, manager):
    monkeypatch.setattr(languages_api, "ADDS_AT_ONCE", 1)
    under_way = threading.Event()
    go_on = threading.Event()

    def slow(rating_key, key):
        under_way.set()
        go_on.wait(5)
        return "40"

    monkeypatch.setattr(manager, "add_subtitle", slow)
    add = {"language": "de", "index": 0}
    alice.get("/api/watch/languages/201/subtitles?language=de")
    first = threading.Thread(
        target=alice.post, args=("/api/watch/languages/201/subtitles",), kwargs={"json": add}
    )
    first.start()
    try:
        assert under_way.wait(5)
        assert alice.post("/api/watch/languages/201/subtitles", json=add).status_code == 429
    finally:
        go_on.set()
        first.join(5)
    assert alice.post("/api/watch/languages/201/subtitles", json=add).status_code == 200


def test_only_two_letter_codes_plex_takes_are_searched():
    bad = {"iw", "fil", "por", "pob", "chi"}
    codes = languages_api.SUBTITLE_LANGUAGES
    assert all(len(c) == 2 and c.islower() and c not in bad for c in codes)
    assert {"he", "pt", "en"} <= set(codes)
