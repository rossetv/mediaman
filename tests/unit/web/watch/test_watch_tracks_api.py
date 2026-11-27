"""Tests for a person's audio and subtitles: per title, and their default languages."""

from __future__ import annotations

import defusedxml.ElementTree as ET
import pytest

from mediaman.services.media_meta.plex_watch import PlexItem, PlexItemNotFound, PlexWatch
from mediaman.web.routes.watch import tracks_api

PLEX_URL = "http://plex-lan.example.test:32400"

# Two episodes of show 199, whose stream ids differ, and a movie in a library not scanned.
STREAMS = {
    "201": (
        '<Stream id="11" streamType="2" languageCode="eng" selected="1"/>'
        '<Stream id="12" streamType="2" languageCode="ger"/>'
        '<Stream id="13" streamType="3" languageCode="eng" hearingImpaired="1"/>'
        '<Stream id="14" streamType="3" languageCode="eng"/>'
    ),
    "202": (
        '<Stream id="21" streamType="2" languageCode="eng" selected="1"/>'
        '<Stream id="22" streamType="2" languageCode="deu"/>'
        '<Stream id="23" streamType="3" languageCode="eng"/>'
        '<Stream id="24" streamType="3" languageCode="eng" hearingImpaired="1"/>'
    ),
}


def _item(key: str) -> PlexItem:
    if key in STREAMS:
        return PlexItem("episode", key, "Ep", "", None, 1500, "1", None, False,
                        grandparent_rating_key="199")  # fmt: skip
    if key == "301":
        return PlexItem("movie", key, "Elsewhere", "", 2020, 6000, "9", None, False)
    raise PlexItemNotFound("Item not found in Plex")


@pytest.fixture
def app(app_factory, conn, monkeypatch):
    for key, value in (("plex_url", PLEX_URL), ("plex_token", "tok"), ("plex_libraries", '["1"]')):
        conn.execute(
            "INSERT INTO settings (key, value, encrypted, updated_at)"
            " VALUES (?, ?, 0, datetime('now'))",
            (key, value),
        )
    conn.commit()
    monkeypatch.setattr(PlexWatch, "get_item", lambda self, key, marks=False: _item(key))
    monkeypatch.setattr(
        PlexWatch,
        "_metadata",
        lambda self, key, query="": (
            None,
            ET.fromstring(f"<Video><Media><Part>{STREAMS.get(key, '')}</Part></Media></Video>"),
        ),
    )
    return app_factory(tracks_api.router, conn=conn)


@pytest.fixture
def alice(app, conn, authed_client):
    return authed_client(app, conn, username="alice")


@pytest.fixture
def bob(app, conn, authed_client):
    return authed_client(app, conn, username="bob")


def _picked(resp) -> tuple[str | None, str | None]:
    assert resp.status_code == 200, resp.text
    return resp.json()["audio"], resp.json()["subtitle"]


def test_a_title_lists_its_tracks_and_what_plays_without_any_choice(alice):
    resp = alice.get("/api/watch/tracks/201")
    assert _picked(resp) == ("11", None)
    body = resp.json()
    assert [t["language"] for t in body["audio_tracks"]] == ["en", "de"]
    assert [t["id"] for t in body["subtitle_tracks"]] == ["13", "14"]
    assert body["remembered"] is False
    assert body["defaults"] == {"audio_lang": None, "subtitle_lang": None}


def test_a_choice_plays_on_the_item_and_carries_to_the_next_episode(alice):
    assert _picked(alice.put("/api/watch/tracks/201", json={"audio": "12", "subtitle": "13"})) == (
        "12",
        "13",
    )
    resp = alice.get("/api/watch/tracks/202")
    # German and SDH English, though the ids differ.
    assert _picked(resp) == ("22", "24")
    assert resp.json()["remembered"] is True


def test_changing_one_track_keeps_the_other(alice):
    alice.put("/api/watch/tracks/201", json={"audio": "12", "subtitle": "14"})
    assert _picked(alice.put("/api/watch/tracks/202", json={"subtitle": "off"})) == ("22", None)
    assert _picked(alice.put("/api/watch/tracks/202", json={"audio": None})) == ("21", None)


def test_going_back_to_the_defaults_forgets_the_choice_for_the_whole_show(alice):
    alice.put("/api/watch/tracks/201", json={"audio": "12"})
    alice.put("/api/account/playback", json={"subtitle_lang": "en"})
    resp = alice.delete("/api/watch/tracks/202")
    assert resp.json()["remembered"] is False
    assert _picked(alice.get("/api/watch/tracks/201")) == ("11", None)


def test_one_person_s_choice_is_not_another_s(alice, bob):
    alice.put("/api/watch/tracks/201", json={"audio": "12"})
    alice.put("/api/account/playback", json={"audio_lang": "de"})
    assert _picked(bob.get("/api/watch/tracks/201")) == ("11", None)
    assert bob.get("/api/account/playback").json()["audio_lang"] is None


@pytest.mark.parametrize("body", [{"audio": "13"}, {"subtitle": "99"}])
def test_a_track_the_item_does_not_have_is_refused_and_nothing_kept(alice, body):
    resp = alice.put("/api/watch/tracks/201", json=body)
    assert (resp.status_code, resp.json()["error"]) == (409, "track_unavailable")
    assert alice.get("/api/watch/tracks/201").json()["remembered"] is False


@pytest.mark.parametrize("key", ["301", "999", "1x"])
def test_only_titles_of_the_scanned_libraries_answer(alice, key):
    assert alice.get(f"/api/watch/tracks/{key}").status_code == 404
    assert alice.put(f"/api/watch/tracks/{key}", json={}).status_code == 404


def test_default_languages_are_kept_in_one_form_and_play(alice):
    resp = alice.put("/api/account/playback", json={"audio_lang": "ger", "subtitle_lang": "EN"})
    assert (resp.json()["audio_lang"], resp.json()["subtitle_lang"]) == ("de", "en")
    assert {"code": "de", "name": "German"} in resp.json()["languages"]
    assert _picked(alice.get("/api/watch/tracks/201")) == ("12", "14")
    resp = alice.put("/api/account/playback", json={"subtitle_lang": "off"})
    assert (resp.json()["audio_lang"], resp.json()["subtitle_lang"]) == ("de", "off")


@pytest.mark.parametrize(
    "body", [{"audio_lang": "xx"}, {"subtitle_lang": "zzz"}, {"audio_lang": "off"}]
)
def test_a_language_not_known_is_refused(alice, body):
    assert alice.put("/api/account/playback", json=body).status_code == 422
    assert alice.get("/api/account/playback").json()["audio_lang"] is None
