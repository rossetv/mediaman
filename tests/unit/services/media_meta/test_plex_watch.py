"""Tests for the read-only Plex metadata reader used by Watch."""

from __future__ import annotations

import pytest
import requests

from mediaman.services.infra import SafeHTTPError
from mediaman.services.media_meta.plex_watch import (
    PlexItem,
    PlexItemNotFound,
    PlexNotConfigured,
    PlexUnavailable,
    PlexWatch,
    is_in_libraries,
)

TOKEN = "s3cret-token"
BASE = "http://plex.lan:32400"


class _Resp:
    def __init__(self, body: str | bytes, content_type: str = "text/xml") -> None:
        self.content = body.encode() if isinstance(body, str) else body
        self.headers = {"Content-Type": content_type}

    def close(self) -> None:
        pass


class _FakeHTTP:
    """Maps a Plex path to a response, or to an exception to raise."""

    def __init__(self, routes: dict[str, _Resp | Exception]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.options: list[dict[str, object]] = []

    def get(self, url: str, *, headers: dict[str, str], **options: object) -> _Resp:
        self.calls.append((url, headers))
        self.options.append(options)
        result = self.routes[url.removeprefix(BASE)]
        if isinstance(result, Exception):
            raise result
        return result


def _watch(routes: dict[str, _Resp | Exception]) -> tuple[PlexWatch, _FakeHTTP]:
    http = _FakeHTTP(routes)
    return PlexWatch(BASE, TOKEN, http=http), http  # type: ignore[arg-type]


def _container(inner: str, **attrs: str) -> _Resp:
    attr_text = " ".join(f'{k}="{v}"' for k, v in attrs.items())
    return _Resp(f"<MediaContainer {attr_text}>{inner}</MediaContainer>")


def _episode_xml(key: str, index: int, season: int = 1, extra: str = "") -> str:
    return (
        f'<Video ratingKey="{key}" type="episode" title="Ep {index}" index="{index}" '
        f'parentIndex="{season}" parentRatingKey="s{season}" duration="1500000" '
        f'thumb="/library/metadata/{key}/thumb/1">{extra}</Video>'
    )


SHOW_CHILDREN = _container(
    '<Directory ratingKey="s0" type="season" index="0" title="Specials"/>'
    '<Directory ratingKey="11" type="season" index="1" title="Season 1"/>'
    '<Directory ratingKey="12" type="season" index="2" title="Season 2"/>'
)


def _show_routes() -> dict[str, _Resp | Exception]:
    return {
        "/library/metadata/10": _container(
            '<Directory ratingKey="10" type="show" title="Foo" year="2008" summary="A show" '
            'thumb="/library/metadata/10/thumb/5"><Genre tag="Drama"/><Genre tag="Crime"/>'
            "</Directory>",
            librarySectionID="3",
        ),
        "/library/metadata/10/children": SHOW_CHILDREN,
        "/library/metadata/11/children": _container(
            _episode_xml("101", 1).replace(
                'type="episode"', 'type="episode" originallyAvailableAt="2008-01-20"'
            )
            + _episode_xml("102", 2),
        ),
        "/library/metadata/12/children": _container(
            _episode_xml("201", 2, season=2).replace(
                'type="episode"', 'type="episode" originallyAvailableAt="2010-03-01"'
            )
            + _episode_xml("200", 1, season=2),
        ),
    }


def test_not_configured_without_url_or_token() -> None:
    with pytest.raises(PlexNotConfigured):
        PlexWatch(None, TOKEN)
    with pytest.raises(PlexNotConfigured):
        PlexWatch(BASE, "")


def test_get_item_reads_episode_fields_and_sends_token_in_header_only() -> None:
    xml = _container(
        '<Video ratingKey="101" type="episode" title="Pilot" summary="It begins" year="2008" '
        'index="1" parentIndex="1" parentRatingKey="11" grandparentRatingKey="10" '
        'grandparentTitle="Foo" duration="2700000" thumb="/library/metadata/101/thumb/9"/>',
        librarySectionID="3",
    )
    watch, http = _watch({"/library/metadata/101": xml})

    item = watch.get_item("101")

    assert (item.type, item.title, item.year, item.duration_s) == ("episode", "Pilot", 2008, 2700)
    assert (item.index, item.parent_index) == (1, 1)
    assert (item.parent_rating_key, item.grandparent_rating_key) == ("11", "10")
    assert item.grandparent_title == "Foo"
    assert item.library_section_id == "3"
    assert item.thumb == "/library/metadata/101/thumb/9"
    url, headers = http.calls[0]
    assert TOKEN not in url
    assert headers == {"X-Plex-Token": TOKEN}


def _movie_with_streams(streams: str) -> _Resp:
    return _container(
        '<Video ratingKey="7" type="movie" title="Film" duration="60000" librarySectionID="1">'
        f"<Media><Part>{streams}</Part></Media></Video>"
    )


def test_subtitle_selected_true_only_for_a_selected_subtitle_stream() -> None:
    selected = (
        '<Stream streamType="1"/><Stream streamType="2" selected="1"/>'
        '<Stream streamType="3" selected="1" language="English"/>'
    )
    watch, _ = _watch({"/library/metadata/7": _movie_with_streams(selected)})
    assert watch.get_item("7").subtitle_selected is True


def test_subtitle_selected_false_when_subtitle_not_selected_or_audio_selected() -> None:
    streams = '<Stream streamType="2" selected="1"/><Stream streamType="3"/>'
    watch, _ = _watch({"/library/metadata/7": _movie_with_streams(streams)})
    assert watch.get_item("7").subtitle_selected is False


def test_get_item_rejects_non_numeric_key_without_calling_plex() -> None:
    watch, http = _watch({})
    with pytest.raises(PlexItemNotFound):
        watch.get_item("1/../../status")
    assert http.calls == []


def test_get_item_refuses_kinds_watch_cannot_show() -> None:
    xml = _container('<Directory ratingKey="5" type="collection" title="Pile"/>')
    watch, _ = _watch({"/library/metadata/5": xml})
    with pytest.raises(PlexItemNotFound):
        watch.get_item("5")


def test_get_show_excludes_specials_and_reads_details() -> None:
    watch, _ = _watch(_show_routes())

    show = watch.get_show("10")

    assert [(s.rating_key, s.index, s.title) for s in show.seasons] == [
        ("11", 1, "Season 1"),
        ("12", 2, "Season 2"),
    ]
    assert (show.title, show.genre, show.year_first, show.year_last) == ("Foo", "Drama", 2008, 2010)
    assert show.library_section_id == "3"


def test_get_show_on_a_movie_is_not_found() -> None:
    watch, _ = _watch({"/library/metadata/7": _movie_with_streams("")})
    with pytest.raises(PlexItemNotFound):
        watch.get_show("7")


def test_season_episodes_come_back_in_order() -> None:
    watch, _ = _watch(_show_routes())
    episodes = watch.get_season_episodes("12")
    assert [(e.rating_key, e.index) for e in episodes] == [("200", 1), ("201", 2)]
    assert episodes[0].duration_s == 1500
    assert episodes[0].thumb == "/library/metadata/200/thumb/1"


def test_a_season_and_its_episodes_come_with_plexs_own_words_dates_and_count() -> None:
    routes = _show_routes()
    routes["/library/metadata/10/children"] = _container(
        '<Directory ratingKey="11" type="season" index="1" summary=" A start. " leafCount="9"/>'
        '<Directory ratingKey="12" type="season" index="2" leafCount="x"/>'
    )
    routes["/library/metadata/11/children"] = _container(
        '<Video ratingKey="1" type="episode" index="1" summary="It begins. " '
        'originallyAvailableAt="2008-01-20"/>'
        '<Video ratingKey="2" type="episode" index="2" originallyAvailableAt="2008"/>'
    )
    watch, _ = _watch(routes)

    seasons = watch.get_show("10").seasons
    episodes = watch.get_season_episodes("11")

    assert [(s.summary, s.episode_count) for s in seasons] == [("A start.", 9), ("", None)]
    assert [(e.summary, e.aired) for e in episodes] == [("It begins.", "2008-01-20"), ("", None)]


def _next_routes(episode_key: str, season: int, season_key: str) -> dict[str, _Resp | Exception]:
    routes = _show_routes()
    routes[f"/library/metadata/{episode_key}"] = _container(
        f'<Video ratingKey="{episode_key}" type="episode" index="2" '
        f'parentIndex="{season}" parentRatingKey="{season_key}" grandparentRatingKey="10" '
        'title="x"/>',
        librarySectionID="3",
    )
    return routes


def test_next_episode_within_season() -> None:
    routes = _show_routes()
    routes["/library/metadata/101"] = _container(
        '<Video ratingKey="101" type="episode" index="1" parentIndex="1" parentRatingKey="11" '
        'grandparentRatingKey="10" title="x"/>'
    )
    watch, _ = _watch(routes)
    nxt = watch.next_episode("101")
    assert nxt is not None
    assert nxt.rating_key == "102"


def test_next_episode_crosses_into_next_season_skipping_specials() -> None:
    watch, _ = _watch(_next_routes("102", 1, "11"))
    nxt = watch.next_episode("102")
    assert nxt is not None
    assert (nxt.rating_key, nxt.season_index, nxt.index) == ("200", 2, 1)


def test_next_episode_is_none_after_the_last_episode() -> None:
    watch, _ = _watch(_next_routes("201", 2, "12"))
    assert watch.next_episode("201") is None


def test_missing_item_maps_to_not_found() -> None:
    err = SafeHTTPError(404, "", f"{BASE}/library/metadata/9?X-Plex-Token={TOKEN}")
    watch, _ = _watch({"/library/metadata/9": err})
    with pytest.raises(PlexItemNotFound):
        watch.get_item("9")


@pytest.mark.parametrize(
    "failure",
    [
        requests.ConnectionError(f"refused {BASE}/library/metadata/9 X-Plex-Token={TOKEN}"),
        requests.Timeout(f"timed out {BASE} {TOKEN}"),
        SafeHTTPError(503, f"down {TOKEN}", f"{BASE}/library/metadata/9"),
        SafeHTTPError(0, "refused by SSRF guard", BASE),
    ],
)
def test_unreachable_plex_maps_to_unavailable_without_url_or_token(failure: Exception) -> None:
    watch, _ = _watch({"/library/metadata/9": failure})
    with pytest.raises(PlexUnavailable) as caught:
        watch.get_item("9")
    shown = f"{caught.value} {caught.value!r}"
    assert TOKEN not in shown
    assert "plex.lan" not in shown
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_garbled_xml_maps_to_unavailable() -> None:
    watch, _ = _watch({"/library/metadata/9": _Resp("<html>not plex")})
    with pytest.raises(PlexUnavailable):
        watch.get_item("9")


@pytest.mark.parametrize(
    "path",
    [
        "/status/sessions",
        "http://evil.example/library/x",
        "//evil.example/library/x",
        "/library/../status/sessions",
        "/library/metadata/1/thumb/2?X-Plex-Token=abc",
        "/library/%2e%2e/x",
        "",
    ],
)
def test_fetch_image_rejects_paths_outside_library(path: str) -> None:
    watch, http = _watch({})
    with pytest.raises(PlexItemNotFound):
        watch.fetch_image(path)
    assert http.calls == []


def test_fetch_image_returns_bytes_and_a_safe_content_type() -> None:
    path = "/library/metadata/101/thumb/9"
    watch, http = _watch({path: _Resp(b"\xff\xd8jpeg", "image/png; charset=binary")})
    assert watch.fetch_image(path) == (b"\xff\xd8jpeg", "image/png")
    assert http.calls[0][1] == {"X-Plex-Token": TOKEN}


def test_fetch_image_never_passes_on_a_non_image_content_type() -> None:
    path = "/library/metadata/101/thumb/9"
    watch, _ = _watch({path: _Resp(b"<script>", "text/html")})
    assert watch.fetch_image(path)[1] == "image/jpeg"


def test_is_in_libraries_matches_section_ids_as_strings() -> None:
    watch, _ = _watch({"/library/metadata/7": _movie_with_streams("")})
    item = watch.get_item("7")
    assert is_in_libraries(item, {1, 4})
    assert is_in_libraries(item, ["1"])
    assert not is_in_libraries(item, {"2"})
    assert not is_in_libraries(item, set())


def test_unavailable_plex_logs_why_without_the_token(caplog: pytest.LogCaptureFixture) -> None:
    path = "/library/metadata/101/thumb/9"
    failure = SafeHTTPError(200, "response body too large: declared 9000000 > cap 8388608", BASE)
    watch, _ = _watch({path: failure})
    with (
        caplog.at_level("WARNING", logger="mediaman.services.media_meta.plex_watch"),
        pytest.raises(PlexUnavailable),
    ):
        watch.fetch_image(path)
    assert "too large" in caplog.text
    assert TOKEN not in caplog.text


def test_a_token_straddling_the_log_cut_is_still_hidden(caplog: pytest.LogCaptureFixture) -> None:
    path = "/library/metadata/101/thumb/9"
    watch, _ = _watch({path: SafeHTTPError(500, "x" * 115 + TOKEN, BASE)})
    with (
        caplog.at_level("WARNING", logger="mediaman.services.media_meta.plex_watch"),
        pytest.raises(PlexUnavailable),
    ):
        watch.fetch_image(path)
    assert TOKEN[:5] not in caplog.text


MARKS = "?includeMarkers=1&includeChapters=1"


def _marked_movie(inner: str, duration: str = "600000") -> PlexItem:
    xml = _container(
        f'<Video ratingKey="7" type="movie" title="Film" duration="{duration}">{inner}</Video>',
        librarySectionID="1",
    )
    watch, http = _watch({f"/library/metadata/7{MARKS}": xml})
    item = watch.get_item("7", marks=True)
    assert len(http.calls) == 1
    return item


def _marker(kind: str, start: object, end: object, extra: str = "") -> str:
    return f'<Marker type="{kind}" startTimeOffset="{start}" endTimeOffset="{end}" {extra}/>'


def test_both_spellings_of_credits_are_read_and_other_markers_are_not() -> None:
    item = _marked_movie(
        _marker("credits", 500000, 600000, 'final="1"')
        + _marker("credit", 300000, 310000)
        + _marker("commercial", 100000, 130000)
        + _marker("intro", 5000, 65500)
    )
    assert [(m.kind, m.start_s, m.end_s, m.final) for m in item.markers] == [
        ("intro", 5.0, 65.5, False),
        ("credits", 300.0, 310.0, False),
        ("credits", 500.0, 600.0, True),
    ]


def test_markers_are_clamped_to_the_item_and_unusable_ones_dropped() -> None:
    item = _marked_movie(
        _marker("intro", -4000, 30000)
        + _marker("intro", 60000, 50000)
        + _marker("intro", 700000, 800000)
        + _marker("intro", "nan", "inf")
        + _marker("intro", "1", "9" * 400)
    )
    assert [(m.start_s, m.end_s) for m in item.markers] == [(0.0, 30.0), (0.001, 600.0)]


def test_chapters_get_plain_titles_and_a_name_when_plex_has_none() -> None:
    title = "The&#10;  Crossing " + "x" * 200
    item = _marked_movie(
        f'<Chapter tag="{title}" startTimeOffset="0" endTimeOffset="60000"/>'
        '<Chapter tag=" " startTimeOffset="60000" endTimeOffset="120000"/>'
    )
    first, second = item.chapters
    assert first.title.startswith("The Crossing x")
    assert len(first.title) == 120
    assert (second.title, second.start_s, second.end_s) == ("Chapter 2", 60.0, 120.0)


def test_a_flood_of_markers_and_chapters_is_cut() -> None:
    item = _marked_movie(
        "".join(_marker("intro", n, 500000) for n in range(50))
        + "".join(f'<Chapter startTimeOffset="{n}" endTimeOffset="500000"/>' for n in range(500))
    )
    assert len(item.markers) == 20
    assert len(item.chapters) == 200


def test_the_plain_item_read_asks_for_no_markers() -> None:
    watch, http = _watch({"/library/metadata/7": _movie_with_streams("")})
    watch.get_item("7")
    assert http.calls[0][0] == f"{BASE}/library/metadata/7"


def _movie_with_part(part: str, duration: str = "62000") -> PlexItem:
    xml = _container(
        f'<Video ratingKey="7" type="movie" title="Film" duration="{duration}">'
        f'<Media><Part size="9" {part}/></Media></Video>'
    )
    return _watch({"/library/metadata/7": xml})[0].get_item("7")


@pytest.mark.parametrize(
    ("part", "duration", "count"),
    [
        ('id="3" indexes="sd"', "62000", 12),
        ('id="3" indexes="hd,sd"', "62000", 12),
        ('id="3" indexes="sd" duration="51000"', "62000", 10),
        ('id="3" indexes="sd" duration="90000"', "62000", 12),
        ('id="3" indexes="sd" duration="59900"', "59900", 11),
        ('id="3" indexes="sd"', "3000", 1),
        ('id="3"', "62000", 0),
        ('id="../x" indexes="sd"', "62000", 0),
        ('id="3" indexes="sd"', "", 0),
        ('id="3" indexes="sd"', "9" * 15, 17280),
    ],
    ids=[
        "sd",
        "among others",
        "file shorter than the title",
        "file longer than the title",
        "title rounded up",
        "under one interval",
        "none made",
        "odd part id",
        "no duration",
        "absurd duration",
    ],
)
def test_preview_frames_are_counted_up_to_the_last_whole_interval(part, duration, count) -> None:
    assert _movie_with_part(part, duration).preview_count == count


@pytest.mark.parametrize(
    ("stream", "plain"),
    [
        ('codec="h264" bitDepth="8"', True),
        ('codec="h264" bitDepth="8" scanType="progressive" chromaSubsampling="4:2:0"', True),
        ('codec="hevc" bitDepth="8"', False),
        ('codec="h264" bitDepth="10"', False),
        ('codec="h264"', False),
        ('codec="h264" bitDepth="8" scanType="interlaced"', False),
        ('codec="h264" bitDepth="8" colorTrc="smpte2084"', False),
        ('codec="h264" bitDepth="8" DOVIPresent="1"', False),
        ('codec="h264" bitDepth="8" chromaSubsampling="4:2:2"', False),
    ],
)
def test_only_eight_bit_sdr_progressive_h264_counts_as_plain(stream, plain) -> None:
    watch, _ = _watch(
        {"/library/metadata/7": _movie_with_streams(f'<Stream streamType="1" {stream}/>')}
    )
    media = watch.get_item("7").media
    assert media is not None
    assert media.plain_h264 is plain


def test_a_preview_frame_is_read_from_the_frames_plex_already_made() -> None:
    frame = b"\xff\xd8\xff\xe0frame"
    watch, http = _watch({"/library/parts/31/indexes/sd/65000": _Resp(frame, "image/jpeg")})
    assert watch.fetch_preview("31", 65000) == frame
    assert http.options[0]["max_bytes"] == 256 * 1024
    assert http.options[0]["expected_content_type"] == "image/jpeg"


def test_a_preview_that_is_not_a_jpeg_is_refused() -> None:
    watch, _ = _watch({"/library/parts/31/indexes/sd/0": _Resp(b"<html>", "image/jpeg")})
    with pytest.raises(PlexUnavailable):
        watch.fetch_preview("31", 0)


def test_a_preview_of_an_odd_part_id_never_reaches_plex() -> None:
    watch, http = _watch({})
    with pytest.raises(PlexItemNotFound):
        watch.fetch_preview("31/../..", 0)
    assert http.calls == []
