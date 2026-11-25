"""Playing on an older TV: what it may reach, what it believes, what it says."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mediaman.services.dlna.addresses import own_networks
from mediaman.services.dlna.errors import (
    TvBadAnswer,
    TvError,
    TvFault,
    TvRefused,
    TvUnreachable,
)
from mediaman.services.dlna.media import Media
from mediaman.services.dlna.renderer import Renderer, Status, describe, find
from mediaman.services.dlna.wire import MAX_BODY
from tests.unit.services.dlna.fake_tv import (
    AVT,
    DESCRIPTION,
    LOCATION,
    TV,
    FakeTv,
    fault,
    http,
    soap,
    ssdp,
)


@pytest.fixture
def tv() -> FakeTv:
    return FakeTv()


@pytest.fixture
def found(tv: FakeTv) -> Renderer:
    return find(TV, tv.wire())


# --- the address -----------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "169.254.169.254",
        "100.100.100.200",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.251",
        "239.255.255.250",
        "255.255.255.255",
        "8.8.8.8",
        "192.0.2.1",
        "172.32.0.1",
        "172.17.0.1",  # the container's own network
        "::1",
        "fd00::1",
        "::ffff:192.168.1.20",
        "[::ffff:192.168.1.20]",
        "tv.local",
        "localhost",
        "192.168.1.20.nip.io",
        "192.168.1",
        "0xc0.0xa8.1.20",
        "0300.0250.1.20",
        "3232235796",
        "192.168.1.020",
        "192.168.1.20:80",
        "192.168.1.20/24",
        "http://192.168.1.20",
        "user@192.168.1.20",
        " 192.168.1.20",
        "192.168.1.20\n",
        "１92.168.1.20",
        "",
    ],
)
def test_an_address_that_is_not_a_literal_home_one_is_never_contacted(
    tv: FakeTv, address: str
) -> None:
    with pytest.raises(TvRefused):
        find(address, tv.wire())
    with pytest.raises(TvRefused):
        describe(address, f"http://{address}:7676/x", tv.wire())
    with pytest.raises(TvRefused):
        tv.wire().request(address, 7676, "/x")
    with pytest.raises(TvRefused):
        tv.wire().search(address, lambda _url: None)
    assert tv.connects == []
    assert tv.sent_udp == []


@pytest.mark.parametrize("address", ["10.0.0.5", "172.16.0.9", "172.31.255.9", "192.168.1.20"])
def test_a_home_address_is_allowed(tv: FakeTv, address: str) -> None:
    assert tv.wire().check(address) == address


def test_a_tv_whose_network_becomes_the_containers_own_is_refused(
    tv: FakeTv, found: Renderer, tmp_path: Path
) -> None:
    tv.own = own_networks(_route_table(tmp_path, "0001A8C0", "00FFFFFF"))
    connects = len(tv.connects)
    with pytest.raises(TvRefused):
        found.play()
    assert len(tv.connects) == connects


def _route_table(folder: Path, destination: str, mask: str) -> Path:
    table = folder / "route"
    table.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "ppp0\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n"
        "eth0\t0000000A\t010011AC\t0003\t0\t0\t0\t000000FF\t0\t0\t0\n"
        f"eth0\t{destination}\t00000000\t0001\t0\t0\t0\t{mask}\t0\t0\t0\n"
    )
    return table


def test_the_route_table_gives_the_networks_mediaman_is_on(tmp_path: Path) -> None:
    table = _route_table(tmp_path, "000011AC", "0000FFFF")
    assert [str(net) for net in own_networks(table)] == ["172.17.0.0/16"]


def test_every_address_is_refused_when_the_route_table_cannot_be_read(tmp_path: Path) -> None:
    with pytest.raises(TvRefused):
        own_networks(tmp_path / "missing")
    (tmp_path / "odd").write_text("header\nnonsense\n")
    with pytest.raises(TvRefused):
        own_networks(tmp_path / "odd")


# --- where an answer points ------------------------------------------------

ELSEWHERE = [
    "http://192.168.1.21:7676/x",
    "http://127.0.0.1:7676/x",
    "http://169.254.169.254/latest",
    "http://tv.local:7676/x",
    "http://[::1]:7676/x",
    f"https://{TV}:7676/x",
    f"file://{TV}:7676/etc/passwd",
    f"http://{TV}/x",
    f"http://{TV}:22/x",
    f"http://{TV}:6379/x",
    f"http://{TV}:99999/x",
    f"http://{TV}:7676@192.168.1.21:7676/x",
    f"http://u:p@{TV}:7676/x",
    f"http://{TV}.evil.example:7676/x",
    f"http://{TV}:7676/x#y",
    f"http://{TV}:7676\\@evil/x",
    "http://[",
    f"http://{TV}:7676/" + "a" * 2000,
]


@pytest.mark.parametrize("url", ELSEWHERE)
def test_a_location_off_the_tv_is_never_fetched(tv: FakeTv, url: str) -> None:
    tv.datagrams = [ssdp(url)]
    with pytest.raises((TvRefused, TvBadAnswer)):
        find(TV, tv.wire())
    with pytest.raises((TvRefused, TvBadAnswer)):
        describe(TV, url, tv.wire())
    assert tv.connects == []


@pytest.mark.parametrize(
    "url", [*ELSEWHERE, "//192.168.1.21:7676/x", "/a b", "/a&#13;&#10;Evil: 1", ""]
)
@pytest.mark.parametrize("where", ["control", "base"])
def test_a_control_address_off_the_tv_is_refused(tv: FakeTv, url: str, where: str) -> None:
    if where == "control":
        page = DESCRIPTION.replace("/smp_22_", url)
    elif url:
        page = DESCRIPTION.replace("<device>", f"<URLBase>{url}</URLBase><device>")
    else:
        return
    tv.pages["/smp_14_"] = http(page)
    with pytest.raises((TvRefused, TvBadAnswer)):
        find(TV, tv.wire())


def test_a_port_off_the_list_is_never_connected_to(tv: FakeTv) -> None:
    with pytest.raises(TvRefused):
        tv.wire().request(TV, 22, "/x")
    assert tv.connects == []


@pytest.mark.parametrize("status", ["301 Moved", "302 Found", "307 Temporary", "308 Permanent"])
def test_a_redirect_is_not_followed(tv: FakeTv, status: str) -> None:
    tv.pages["/smp_14_"] = http("", status, f"Location: http://{TV}:7676/other\r\n")
    with pytest.raises(TvRefused):
        find(TV, tv.wire())
    assert len(tv.connects) == 1


def test_a_discovery_answer_from_another_machine_is_ignored(tv: FakeTv) -> None:
    tv.datagrams = [ssdp(f"http://{TV}:7676/fake", "192.168.1.66"), ssdp(LOCATION)]
    assert find(TV, tv.wire()).location == LOCATION


def test_a_bad_discovery_answer_does_not_hide_a_good_one(tv: FakeTv) -> None:
    tv.datagrams = [(b"junk\xff", (TV, 1900)), ssdp("http://10.0.0.1:7676/x"), ssdp(LOCATION)]
    assert find(TV, tv.wire()).location == LOCATION


def test_a_flood_of_discovery_answers_is_given_up_on(tv: FakeTv) -> None:
    tv.datagrams = [(b"junk", (TV, 1900))] * 500
    with pytest.raises(TvUnreachable):
        find(TV, tv.wire())
    assert len(tv.datagrams) > 400


def test_discovery_goes_to_the_tv_alone(tv: FakeTv, found: Renderer) -> None:
    ((message, target),) = tv.sent_udp
    assert target == (TV, 1900)
    assert b'MAN: "ssdp:discover"' in message
    assert b"MediaRenderer:1" in message


# --- size, time and XML ----------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        http(DESCRIPTION + " " * (MAX_BODY + 1 - len(DESCRIPTION))),
        b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 5_000_000,
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4C4B40\r\n" + b"x" * 5_000_000,
        b"HTTP/1.1 200 OK\r\n" + b"A: " + b"x" * 5_000_000,
        b"HTTP/1.1 200 OK\r\n" + (b"A: " + b"x" * 60_000 + b"\r\n") * 50,
        b"HTTP/1.1 100 Continue\r\n\r\n" * 100_000,
    ],
)
def test_an_oversized_answer_is_cut_off(tv: FakeTv, answer: bytes) -> None:
    tv.pages["/smp_14_"] = answer
    with pytest.raises(TvBadAnswer):
        find(TV, tv.wire())
    assert tv.bytes_read < 120_000
    assert tv.closed == 2


def test_an_answer_of_exactly_the_limit_is_read(tv: FakeTv) -> None:
    tv.pages["/smp_14_"] = http(DESCRIPTION + " " * (MAX_BODY - len(DESCRIPTION)))
    assert find(TV, tv.wire()).has_volume


def test_an_answer_that_drips_is_given_up_on_after_three_seconds(tv: FakeTv) -> None:
    tv.seconds_per_read, tv.read_size = 0.5, 1
    started = tv.now
    with pytest.raises(TvUnreachable):
        describe(TV, LOCATION, tv.wire())
    assert tv.now - started <= 3.0


def test_a_connection_that_takes_all_the_time_leaves_none_for_the_answer(tv: FakeTv) -> None:
    tv.seconds_to_connect = 5.0
    with pytest.raises(TvUnreachable):
        describe(TV, LOCATION, tv.wire())
    assert tv.bytes_read == 0


def test_discovery_answers_that_drip_are_given_up_on_after_three_seconds(tv: FakeTv) -> None:
    tv.seconds_per_read = 0.5
    tv.datagrams = [(b"junk", (TV, 1900))] * 100
    started = tv.now
    with pytest.raises(TvUnreachable):
        find(TV, tv.wire())
    assert tv.now - started <= 3.0


@pytest.mark.parametrize(
    "body",
    [
        '<!DOCTYPE r [<!ENTITY a "ha"><!ENTITY b "&a;&a;&a;">]>' + DESCRIPTION[21:].lstrip(),
        f'<!DOCTYPE r SYSTEM "http://{TV}:7676/x.dtd">' + DESCRIPTION[21:].lstrip(),
        '<!DOCTYPE r [<!ENTITY a SYSTEM "file:///etc/passwd">]><root>&a;</root>',
        "<a>" * 20_000,
        '<?xml version="1.0" encoding="nonsense"?><root/>',
        "<root/>",
        "not xml",
        "",
    ],
)
def test_hostile_or_broken_xml_is_a_bad_answer(tv: FakeTv, body: str) -> None:
    tv.pages["/smp_14_"] = http(body)
    with pytest.raises(TvBadAnswer):
        describe(TV, LOCATION, tv.wire())
    tv.pages["/smp_14_"] = http(DESCRIPTION)
    renderer = describe(TV, LOCATION, tv.wire())
    tv.actions["Play"] = http(body)
    with pytest.raises(TvBadAnswer):
        renderer.play()


@pytest.mark.parametrize(
    "answer",
    [
        b"junk\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n",
        http(DESCRIPTION, headers="Content-Encoding: gzip\r\n"),
        http(DESCRIPTION, "404 Not Found"),
    ],
)
def test_a_broken_http_answer_is_a_bad_answer(tv: FakeTv, answer: bytes) -> None:
    tv.pages["/smp_14_"] = answer
    with pytest.raises(TvBadAnswer):
        find(TV, tv.wire())


def test_an_answer_cut_short_counts_as_gone(tv: FakeTv) -> None:
    tv.pages["/smp_14_"] = http(DESCRIPTION)[:-1]
    with pytest.raises(TvUnreachable):
        find(TV, tv.wire())


# --- the description -------------------------------------------------------


def test_the_name_is_one_clean_short_line(tv: FakeTv, found: Renderer) -> None:
    assert found.name == "[TV] Living room"
    tv.datagrams = [ssdp(LOCATION)]
    tv.pages["/smp_14_"] = http(DESCRIPTION.replace("[TV]", "‮" + "x" * 5000))
    name = find(TV, tv.wire()).name
    assert len(name) == 80
    assert name.isprintable()


def test_a_renderer_inside_another_device_is_found(tv: FakeTv) -> None:
    outer = (
        "<device><deviceType>urn:schemas-upnp-org:device:Basic:1</deviceType><deviceList><device>"
    )
    tv.pages["/smp_14_"] = http(
        DESCRIPTION.replace("<device>", outer, 1).replace(
            "</device>", "</device></deviceList></device>"
        )
    )
    find(TV, tv.wire()).play()
    assert tv.requests[-1].startswith(b"POST /smp_22_ ")


def test_controls_may_be_on_another_allowed_port_of_the_tv(tv: FakeTv) -> None:
    tv.pages["/smp_14_"] = http(DESCRIPTION.replace("/smp_22_", f"http://{TV}:9197/avt"))
    find(TV, tv.wire()).play()
    assert tv.connects[-1] == (TV, 9197)


# --- control ---------------------------------------------------------------

FILM = Media(
    url="http://192.168.1.10:8000/api/cast/t/film.ts?a=1&b=2",
    title='Tom & Jerry <"it\'s"> ]]></CurrentURIMetaData>\x00\x0b\ud800',
    seconds=3725,
    subtitle_url="http://192.168.1.10:8000/s.srt?a=1&b=2",
)


def _body(request: bytes) -> ET.Element:
    return ET.fromstring(request.split(b"\r\n\r\n", 1)[1])


def test_a_title_cannot_break_out_of_the_request(tv: FakeTv, found: Renderer) -> None:
    found.load(FILM)
    (action,) = _body(tv.requests[-1])[0]
    assert action.tag == f"{{{AVT}}}SetAVTransportURI"
    assert [e.tag for e in action] == ["InstanceID", "CurrentURI", "CurrentURIMetaData"]
    assert action[1].text == FILM.url
    (item,) = ET.fromstring(action[2].text or "")
    title = item.find("{http://purl.org/dc/elements/1.1/}title")
    assert title is not None
    assert title.text == 'Tom & Jerry <"it\'s"> ]]></CurrentURIMetaData>'
    res = item.find("{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}res")
    assert res is not None
    assert res.text == FILM.url
    assert res.get("duration") == "1:02:05.000"
    assert res.get("protocolInfo") == f"http-get:*:video/mpeg:{FILM.content_features}"
    caption = item.find("{http://www.sec.co.kr/}CaptionInfoEx")
    assert caption is not None
    assert caption.text == FILM.subtitle_url


@pytest.mark.parametrize(
    ("command", "action", "fields"),
    [
        (lambda r: r.play(), "Play", {"Speed": "1"}),
        (lambda r: r.pause(), "Pause", {}),
        (lambda r: r.stop(), "Stop", {}),
        (lambda r: r.seek(3725), "Seek", {"Unit": "REL_TIME", "Target": "1:02:05"}),
        (lambda r: r.seek(-5), "Seek", {"Unit": "REL_TIME", "Target": "0:00:00"}),
        (lambda r: r.set_volume(30), "SetVolume", {"Channel": "Master", "DesiredVolume": "30"}),
        (lambda r: r.set_volume(900), "SetVolume", {"Channel": "Master", "DesiredVolume": "100"}),
    ],
)
def test_each_command_is_the_action_upnp_defines(
    tv: FakeTv,
    found: Renderer,
    command,  # type: ignore[no-untyped-def]
    action: str,
    fields: dict[str, str],
) -> None:
    command(found)
    (sent,) = _body(tv.requests[-1])[0]
    assert sent.tag.endswith("}" + action)
    assert {e.tag: e.text for e in sent} == {"InstanceID": "0", **fields}
    service = sent.tag[1:].split("}")[0]
    assert f'SOAPACTION: "{service}#{action}"'.encode() in tv.requests[-1]


def test_status_is_read_by_asking(tv: FakeTv, found: Renderer) -> None:
    tv.actions["GetTransportInfo"] = soap(
        "GetTransportInfo",
        "<CurrentTransportState>PLAYING</CurrentTransportState>"
        "<CurrentTransportStatus>ERROR_OCCURRED</CurrentTransportStatus>",
    )
    tv.actions["GetPositionInfo"] = soap(
        "GetPositionInfo", "<RelTime>0:10:05.250</RelTime><TrackDuration>01:30:00</TrackDuration>"
    )
    assert found.status() == Status("PLAYING", True, 605, 5400)


def test_values_a_tv_makes_up_are_not_passed_on(tv: FakeTv, found: Renderer) -> None:
    tv.actions["GetTransportInfo"] = soap(
        "GetTransportInfo", "<CurrentTransportState>&lt;b&gt;</CurrentTransportState>"
    )
    tv.actions["GetPositionInfo"] = soap(
        "GetPositionInfo",
        "<RelTime>NOT_IMPLEMENTED</RelTime><TrackDuration>0:99:00</TrackDuration>",
    )
    assert found.status() == Status("UNKNOWN", False, None, None)
    tv.actions["GetVolume"] = soap("GetVolume", "<CurrentVolume>-1</CurrentVolume>")
    with pytest.raises(TvBadAnswer):
        found.volume()
    tv.actions["GetVolume"] = soap("GetVolume", "<CurrentVolume>900</CurrentVolume>")
    assert found.volume() == 100


def test_a_tv_without_volume_control_says_so_plainly(tv: FakeTv) -> None:
    tv.pages["/smp_14_"] = http(DESCRIPTION.replace("RenderingControl:1", "Other:1"))
    renderer = find(TV, tv.wire())
    assert not renderer.has_volume
    with pytest.raises(TvFault):
        renderer.volume()


@pytest.mark.parametrize(
    ("code", "word"),
    [
        ("701", "right now"),
        ("704", "kind of video"),
        ("705", "locked"),
        ("711", "jump"),
        ("714", "kind of video"),
        ("716", "reach the video"),
        ("999", "could not do that"),
        ("evil", "could not do that"),
        ("", "could not do that"),
    ],
)
def test_a_fault_is_told_in_plain_words_never_the_tvs_own(
    tv: FakeTv, found: Renderer, code: str, word: str
) -> None:
    tv.actions["Play"] = fault(code, "SECRET&lt;script&gt;")
    with pytest.raises(TvFault) as caught:
        found.play()
    assert word in str(caught.value)
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize("status", ["401 Unauthorized", "403 Forbidden"])
def test_a_tv_that_has_not_allowed_mediaman_says_what_to_do(
    tv: FakeTv, found: Renderer, status: str
) -> None:
    tv.actions["Play"] = http("", status)
    with pytest.raises(TvFault, match="Allow it on the TV"):
        found.play()


def test_a_tv_that_vanishes_mid_film_is_unreachable_and_can_come_back(
    tv: FakeTv, found: Renderer
) -> None:
    found.load(FILM)
    found.play()
    tv.off = True
    with pytest.raises(TvUnreachable):
        found.status()
    with pytest.raises(TvUnreachable):
        found.stop()
    tv.off = False
    tv.actions["GetTransportInfo"] = b""
    with pytest.raises(TvError):
        found.status()
    del tv.actions["GetTransportInfo"]
    assert found.status().state == "UNKNOWN"


def test_a_tv_that_is_off_when_looked_for_is_unreachable(tv: FakeTv) -> None:
    tv.datagrams = []
    with pytest.raises(TvUnreachable):
        find(TV, tv.wire())


# --- the stream ------------------------------------------------------------


@pytest.mark.parametrize(("ranges", "operation"), [(True, "01"), (False, "00")])
def test_byte_seeking_is_promised_only_when_the_stream_has_it(ranges: bool, operation: str) -> None:
    media = Media(url="http://192.168.1.10/f.ts", title="x", byte_ranges=ranges)
    assert f"DLNA.ORG_OP={operation};" in media.content_features
    flags = media.content_features.rpartition("=")[2]
    assert len(flags) == 32


def test_a_tv_gets_the_stream_headers_it_asks_for() -> None:
    assert FILM.stream_headers({"getContentFeatures.DLNA.org": "1", "getCaptionInfo.sec": "1"}) == {
        "transferMode.dlna.org": "Streaming",
        "contentFeatures.dlna.org": FILM.content_features,
        "CaptionInfo.sec": FILM.subtitle_url,
    }
    assert FILM.stream_headers({}) == {"transferMode.dlna.org": "Streaming"}


@pytest.mark.parametrize(
    "wrong",
    [
        {"url": "file:///etc/passwd"},
        {"url": "http://a/\r\nEvil: 1"},
        {"subtitle_url": "http://a/\n"},
        {"mime": "video/mpeg:*\r\n"},
    ],
)
def test_media_that_could_forge_a_header_is_refused(wrong: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="a TV needs"):
        Media(**{"url": "http://a/f.ts", "title": "x", **wrong})
