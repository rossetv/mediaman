"""mediaman only reads from Plex: every request its Plex clients send is checked against what reads look like.

The one write allowed is selecting the audio and subtitle tracks of a part, which a stream start
does for the moment Plex decides and then puts back.
"""

from __future__ import annotations

import io
import re
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from mediaman.services.cast.tracks import read_tracks
from mediaman.services.infra.http import client as http_client
from mediaman.services.media_meta import held_selections, plex_stream
from mediaman.services.media_meta import plex as plex_module
from mediaman.services.media_meta.plex import PlexClient
from mediaman.services.media_meta.plex_stream import (
    IDLE_TIMEOUT_SECONDS,
    SegmentRead,
    StreamManager,
    Tracks,
    TvTracks,
)
from mediaman.services.media_meta.plex_watch import PlexWatch

PLEX_URL = "http://plex.test:32400"
TOKEN = "SECRET-TOKEN-123"
TRANSCODE = "/video/:/transcode/universal"

_READS = [
    re.compile(p)
    for p in (
        r"/",
        r"/identity",
        r"/library",
        r"/library/sections",
        r"/library/sections/\d+/(all|collections)",
        r"/library/metadata/\d+",
        r"/library/metadata/\d+/children",
        r"/library/metadata/\d+/thumb/\d+",
        r"/library/parts/\d+/indexes/sd/\d+",
        r"/status/sessions/history/all",
        r"/accounts",
        r"/transcode/sessions",
        re.escape(TRANSCODE) + r"/(decision|start\.m3u8|ping|stop)",
        re.escape(TRANSCODE) + r"/session/[\w-]+/base/[\w.-]+",
    )
]
_TRACK_PARAMS = {"audioStreamID", "subtitleStreamID"}

_MOVIE = (
    '<Video ratingKey="123" key="/library/metadata/123" type="movie" title="Film" year="2020"'
    ' addedAt="1600000000" duration="6000000" librarySectionID="1"'
    ' thumb="/library/metadata/123/thumb/1"><Media id="1" width="1920" height="1080"'
    ' videoResolution="1080"><Part id="55" file="/media/movies/Film (2020)/Film.mkv"'
    ' size="1000" duration="6000000" indexes="sd"><Stream id="5" streamType="1" codec="h264"/>'
    '<Stream id="6" streamType="2" selected="1" languageCode="eng"/>'
    '<Stream id="7" streamType="2" languageCode="fra"/>'
    '<Stream id="9" streamType="3" languageCode="eng"/></Part></Media></Video>'
)
_SHOW = '<Directory ratingKey="200" key="/library/metadata/200/children" type="show" title="Show"/>'
_SEASON = (
    '<Directory ratingKey="201" key="/library/metadata/201/children" type="season" index="1"'
    ' parentRatingKey="200" parentTitle="Show" title="Season 1" addedAt="1600000000"/>'
)
_EPISODE = (
    '<Video ratingKey="202" key="/library/metadata/202" type="episode" title="Pilot" index="1"'
    ' parentIndex="1" parentRatingKey="201" grandparentRatingKey="200" addedAt="1600000000">'
    '<Media id="2"><Part id="56" file="/media/tv/Show/Season 1/Pilot.mkv" size="500"/></Media>'
    "</Video>"
)
_CHILD_PLAYLIST = "#EXTM3U\n#EXT-X-TARGETDURATION:8\n#EXTINF:8.0,\n00000.ts\n#EXT-X-ENDLIST\n"


class _Plex:
    """Answers each Plex request as Plex would, and records it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], bool]] = []
        self.selection = {"audioStreamID": "6", "subtitleStreamID": "0"}

    def _body(self, method: str, path: str, params: dict[str, str]) -> tuple[int, str, str]:
        xml = "text/xml"
        if method == "PUT" and path == "/library/parts/55":
            self.selection.update(params)
            return 200, "", xml
        if path == "/":
            return 200, '<MediaContainer machineIdentifier="m1" friendlyName="Plex"/>', xml
        if path == "/library":
            return 200, '<MediaContainer title1="Plex Library"/>', xml
        if path == "/library/sections":
            return (
                200,
                '<MediaContainer><Directory key="1" type="movie" title="Films"/>'
                '<Directory key="2" type="show" title="Shows"/></MediaContainer>',
                xml,
            )
        if path == "/library/sections/1/all":
            return 200, f'<MediaContainer librarySectionID="1">{_MOVIE}</MediaContainer>', xml
        if path == "/library/sections/2/all":
            return 200, f'<MediaContainer librarySectionID="2">{_SHOW}</MediaContainer>', xml
        if path == "/library/metadata/200":
            return 200, f"<MediaContainer>{_SHOW}</MediaContainer>", xml
        if path == "/library/metadata/200/children":
            return 200, f"<MediaContainer>{_SEASON}</MediaContainer>", xml
        if path == "/library/metadata/201":
            return 200, f"<MediaContainer>{_SEASON}</MediaContainer>", xml
        if path == "/library/metadata/201/children":
            return 200, f"<MediaContainer>{_EPISODE}</MediaContainer>", xml
        if path == "/library/metadata/202":
            return 200, f"<MediaContainer>{_EPISODE}</MediaContainer>", xml
        if path == "/library/metadata/123":
            audio, subtitle = self.selection["audioStreamID"], self.selection["subtitleStreamID"]
            movie = (
                _MOVIE.replace(' selected="1"', "")
                .replace(f'<Stream id="{audio}"', f'<Stream id="{audio}" selected="1"')
                .replace(f'<Stream id="{subtitle}"', f'<Stream id="{subtitle}" selected="1"')
            )
            return 200, f'<MediaContainer librarySectionID="1">{movie}</MediaContainer>', xml
        if path == "/status/sessions/history/all":
            return (
                200,
                '<MediaContainer><Video viewedAt="1600000500" accountID="1"/></MediaContainer>',
                xml,
            )
        if path == "/accounts":
            return 200, '<MediaContainer><Account id="2" name="viewer"/></MediaContainer>', xml
        if path == "/identity":
            return 200, '<MediaContainer machineIdentifier="m1"/>', xml
        if path == "/library/metadata/123/thumb/1":
            return 200, "\x89PNG", "image/png"
        if path.startswith("/library/parts/55/indexes/sd/"):
            return 200, "\xff\xd8\xff\xe0jpeg", "image/jpeg"
        if path == f"{TRANSCODE}/decision":
            audio, subtitle = self.selection["audioStreamID"], self.selection["subtitleStreamID"]
            burned = f'<Stream id="{subtitle}" streamType="3" decision="burn"/>'
            return (
                200,
                f'<MediaContainer><Media><Stream id="{audio}" streamType="2" selected="1"/>'
                f"{burned if subtitle != '0' else ''}</Media></MediaContainer>",
                xml,
            )
        if path == f"{TRANSCODE}/start.m3u8":
            session = params["session"]
            master = (
                f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=8000000\nsession/{session}/base/index.m3u8\n"
            )
            return 200, master, "application/vnd.apple.mpegurl"
        if path.endswith("/base/index.m3u8"):
            return 200, _CHILD_PLAYLIST, "application/vnd.apple.mpegurl"
        if path.endswith(".ts"):
            return 200, "segment", "video/mp2t"
        if path in (f"{TRANSCODE}/ping", f"{TRANSCODE}/stop"):
            return 200, "", xml
        if path == "/transcode/sessions":
            return (
                200,
                '{"MediaContainer": {"TranscodeSession": [{"key": "mediaman-old"}]}}',
                "application/json",
            )
        # Plex accepts what it does not know here, so that only the check below can fail on it.
        return 200, "<MediaContainer/>", xml

    def request(self, method, url, params=None, data=None, json=None, **kwargs):
        parts = urlsplit(url)
        asked = dict(parse_qsl(parts.query)) | {k: str(v) for k, v in dict(params or {}).items()}
        method = method.upper()
        self.calls.append((method, parts.path, asked, data is not None or json is not None))
        status, body, content_type = self._body(method, parts.path, asked)
        response = requests.Response()
        response.status_code = status
        response.headers = CaseInsensitiveDict({"Content-Type": content_type})
        response.raw = io.BytesIO(body.encode("latin-1"))
        response.url = url
        response.encoding = "utf-8"
        return response


@pytest.fixture
def plex(monkeypatch) -> _Plex:
    fake = _Plex()

    def request(_session, method, url, **kwargs):
        return fake.request(method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", request)
    allow = lambda url, **kwargs: (True, None, None)  # noqa: E731
    for module in (http_client, plex_module, plex_stream):
        monkeypatch.setattr(module, "resolve_safe_outbound_url", allow)
    return fake


def _stream(manager: StreamManager, user: str, **tracks: Tracks) -> str:
    stream_id = manager.start(user, "123", "best", True, **tracks)
    manager.playlist(stream_id, user)
    read = SegmentRead(stream_id, user)
    manager.claim_read(read)
    try:
        manager.segment(read, "00000.ts")
    finally:
        manager.release_read(read)
    return stream_id


def _writes_outside_track_selection(calls) -> list[tuple[str, str, dict[str, str]]]:
    wrong = []
    for method, path, params, has_body in calls:
        read = method == "GET" and not has_body and any(p.fullmatch(path) for p in _READS)
        selects = (
            method == "PUT"
            and not has_body
            and re.fullmatch(r"/library/parts/\d+", path) is not None
            and params
            and set(params) <= _TRACK_PARAMS
        )
        if not (read or selects):
            wrong.append((method, path, params))
    return wrong


def test_scan_watch_stream_and_cast_send_plex_only_reads_and_track_selections(plex):
    scanner = PlexClient(PLEX_URL, TOKEN)
    scanner.get_libraries()
    assert len(scanner.get_movie_items("1")) == 1
    assert len(scanner.get_show_seasons("2")) == 1
    scanner.get_watch_history("123")
    scanner.get_season_watch_history("201")
    scanner.get_user_ratings()
    scanner.get_accounts()
    assert scanner.is_reachable()

    watch = PlexWatch(PLEX_URL, TOKEN)
    watch.ping()
    watch.get_item("123", marks=True)
    watch.get_show("200")
    assert [e.title for e in watch.get_season_episodes("201")] == ["Pilot"]
    watch.next_episode("202")
    watch.fetch_image("/library/metadata/123/thumb/1")
    watch.fetch_preview("55", 1000)
    audio, _subtitles = read_tracks(watch, "123")
    assert [t["id"] for t in audio] == ["6", "7"]

    manager = StreamManager(lambda: (PLEX_URL, TOKEN), session=requests.Session())
    manager.stop(_stream(manager, "alice", tracks=Tracks(audio="7", subtitle="9")), "alice")
    manager.end(_stream(manager, "bob", tv=TvTracks(audio="7")), "bob")
    manager.tidy_orphans()

    assert _writes_outside_track_selection(plex.calls) == []
    puts = [(path, params) for method, path, params, _ in plex.calls if method == "PUT"]
    assert puts, "the stream starts should have selected their tracks on Plex"
    assert plex.selection == {"audioStreamID": "6", "subtitleStreamID": "0"}


def test_reaping_shutdown_put_back_and_given_up_starts_send_plex_only_reads_and_track_selections(
    plex, conn
):
    now = [1000.0]
    held_selections.save(conn, "123", "55", {"audioStreamID": "7", "subtitleStreamID": "9"})
    manager = StreamManager(
        lambda: (PLEX_URL, TOKEN), session=requests.Session(), clock=lambda: now[0], db=lambda: conn
    )
    assert manager.put_back_held() == 1

    _stream(manager, "alice")
    now[0] += IDLE_TIMEOUT_SECONDS + 1
    _stream(manager, "bob")
    assert manager.reap() == 1
    manager.stop_all(budget=5.0)

    with manager.starting("carol", "123") as first, manager.starting("carol", "123"):
        assert first.unwanted
    read = SegmentRead("s1", "carol")
    read.cancel()

    assert _writes_outside_track_selection(plex.calls) == []
    asked = {path for _method, path, _params, _ in plex.calls}
    assert {"/library/parts/55", f"{TRANSCODE}/ping", f"{TRANSCODE}/stop"} <= asked
