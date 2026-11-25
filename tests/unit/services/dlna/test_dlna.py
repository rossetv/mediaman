"""Playing on an older TV: what it may reach, what it believes, what it says."""

from __future__ import annotations

import ipaddress
import math
import socket
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mediaman.services.dlna.addresses import check_device_url, own_networks
from mediaman.services.dlna.errors import (
    AnotherTv,
    TvBadAnswer,
    TvError,
    TvFault,
    TvRefused,
    TvUnreachable,
)
from mediaman.services.dlna.media import Media
from mediaman.services.dlna.renderer import Renderer, Status, describe, find, find_again
from mediaman.services.dlna.wire import MAX_BODY, _open_tcp, _open_udp
from tests.unit.services.dlna.fake_tv import (
    AVT,
    DESCRIPTION,
    LOCATION,
    TV,
    VOLUME_RANGE,
    FakeTv,
    MovedTv,
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
    tv.own = own_networks(*_tables(tmp_path, "0001A8C0", "00FFFFFF"))
    connects = len(tv.connects)
    with pytest.raises(TvRefused):
        found.play()
    assert len(tv.connects) == connects


HEADER = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
DEFAULT = "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
ON_LINK = "eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
OWN_ADDRESS = "".join(
    f"""{table}:
  +-- 0.0.0.0/0 3 0 5
     |-- 0.0.0.0
        /0 universe UNICAST
     +-- 127.0.0.0/8 2 0 2
        +-- 127.0.0.0/31 1 0 0
           |-- 127.0.0.0
              /8 host LOCAL
           |-- 127.0.0.1
              /32 host LOCAL
        |-- 127.255.255.255
           /32 link BROADCAST
     +-- 172.17.0.0/16 2 0 2
        +-- 172.17.0.0/30 2 0 2
           |-- 172.17.0.0
              /16 link UNICAST
           |-- 172.17.0.2
              /32 host LOCAL
        |-- 172.17.255.255
           /32 link BROADCAST
"""
    for table in ("Main", "Local")
)


def _tables(
    folder: Path, destination: str, mask: str, own: str = "172.17.0.2"
) -> tuple[Path, Path]:
    routes, local = folder / "route", folder / "fib_trie"
    routes.write_text(
        HEADER
        + DEFAULT
        + "ppp0\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n"
        + "eth0\t0000000A\t010011AC\t0003\t0\t0\t0\t000000FF\t0\t0\t0\n"
        + f"eth0\t{destination}\t00000000\t0001\t0\t0\t0\t{mask}\t0\t0\t0\n"
    )
    local.write_text(OWN_ADDRESS.replace("172.17.0.2", own))
    return routes, local


def test_the_networks_mediaman_is_on_are_those_it_reaches_without_a_gateway(
    tmp_path: Path,
) -> None:
    networks = own_networks(*_tables(tmp_path, "000011AC", "0000FFFF"))
    assert {str(net) for net in networks} == {"172.17.0.0/16", "172.17.0.2/32"}


def test_behind_a_single_address_gateway_mediamans_own_address_is_refused(
    tv: FakeTv, tmp_path: Path
) -> None:
    tv.own = own_networks(*_tables(tmp_path, "0100A8C0", "FFFFFFFF", own=TV))
    with pytest.raises(TvRefused):
        tv.wire().check(TV)
    with pytest.raises(TvRefused):
        tv.wire().check("192.168.0.1")


@pytest.mark.parametrize(
    ("routes", "local"),
    [
        (None, OWN_ADDRESS),
        ("", OWN_ADDRESS),
        (HEADER, OWN_ADDRESS),
        (HEADER + DEFAULT, OWN_ADDRESS),
        (DEFAULT + ON_LINK, OWN_ADDRESS),
        (HEADER + ON_LINK + "nonsense\n", OWN_ADDRESS),
        (HEADER + ON_LINK, None),
        (HEADER + ON_LINK, ""),
        (HEADER + ON_LINK, OWN_ADDRESS.replace("172.17.0.2", "127.0.0.1")),
        (HEADER + ON_LINK, OWN_ADDRESS.replace("172.17.0.2", "nonsense")),
    ],
)
def test_every_address_is_refused_when_mediaman_cannot_tell_its_own_networks(
    tmp_path: Path, routes: str | None, local: str | None
) -> None:
    paths = tmp_path / "route", tmp_path / "fib_trie"
    for path, text in zip(paths, (routes, local), strict=True):
        if text is not None:
            path.write_text(text)
    with pytest.raises(TvRefused, match="container network of its own"):
        own_networks(*paths)


def test_an_address_on_mediamans_own_network_is_refused_with_what_to_do(tv: FakeTv) -> None:
    with pytest.raises(TvRefused, match="container network of its own"):
        tv.wire().check("172.17.0.5")


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
    ("where", "url"),
    [
        *(
            (where, url)
            for where in ("control", "base")
            for url in (*ELSEWHERE, "//192.168.1.21:7676/x", "/a b", "/a&#13;&#10;Evil: 1")
        ),
        ("control", ""),
    ],
)
def test_a_control_address_off_the_tv_is_refused(tv: FakeTv, url: str, where: str) -> None:
    if where == "control":
        page = DESCRIPTION.replace("/smp_22_", url)
    else:
        page = DESCRIPTION.replace("<device>", f"<URLBase>{url}</URLBase><device>")
    tv.pages["/smp_14_"] = http(page)
    with pytest.raises((TvRefused, TvBadAnswer)):
        find(TV, tv.wire())


@pytest.mark.parametrize(
    ("url", "word"),
    [
        (f"http://{TV}/x", "port 80"),
        (f"http://{TV}", "port 80"),
        (f"http://{TV}:22/x", "port 22"),
        (f"https://{TV}:7676/x", "plain http"),
        ("http://10.0.0.1/x", "other than its own"),
        ("http://tv.local:22/x", "other than its own"),
        ("http://[::1]:7676/x", "other than its own"),
        (f"http://{TV}:7676/x#y", "cannot be used"),
        (f"http://u:p@{TV}:7676/x", "cannot be used"),
    ],
)
def test_a_refused_url_is_told_by_its_own_cause(url: str, word: str) -> None:
    with pytest.raises((TvRefused, TvBadAnswer), match=word):
        check_device_url(url, TV)


@pytest.mark.parametrize("port", [7676, 9197, 49152, 49215, 52235, 52323, 55000])
def test_a_port_tvs_use_is_allowed(port: int) -> None:
    assert check_device_url(f"http://{TV}:{port}/x", TV) == (port, "/x")


@pytest.mark.parametrize("port", [80, 443, 8001, 49151, 49216, 52322, 55001])
def test_the_port_beside_one_tvs_use_is_not(port: int) -> None:
    with pytest.raises(TvRefused):
        check_device_url(f"http://{TV}:{port}/x", TV)


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
        tv.wire().search(TV, lambda _url: None)
    assert len(tv.datagrams) > 400


def test_discovery_goes_to_the_tv_alone(tv: FakeTv, found: Renderer) -> None:
    (message, target) = tv.sent_udp[0]
    assert target == (TV, 1900)
    assert b'MAN: "ssdp:discover"' in message
    assert b"MediaRenderer:1" in message


def test_a_lost_discovery_packet_is_sent_again_within_the_same_three_seconds(tv: FakeTv) -> None:
    tv.datagrams = []
    started = tv.now
    with pytest.raises(TvUnreachable):
        tv.wire().search(TV, lambda _url: None)
    assert [target for _, target in tv.sent_udp] == [(TV, 1900)] * 3
    assert tv.now - started == pytest.approx(3.0)


def test_a_reply_to_the_first_discovery_packet_is_enough(tv: FakeTv, found: Renderer) -> None:
    assert len(tv.sent_udp) == 1


@pytest.mark.parametrize(
    "location",
    [
        f"http://{TV}:9197/dmr",
        f"http://{TV}:7676/smp_14_",
        f"http://{TV}:52323/dmr.xml",
        f"http://{TV}:55000/dmr/ddd.xml",
    ],
)
def test_a_tv_deaf_to_discovery_is_looked_for_where_tvs_are_known_to_be(
    tv: FakeTv, location: str
) -> None:
    tv.datagrams = []
    tv.pages = {check_device_url(location, TV)[1]: http(DESCRIPTION)}
    assert find(TV, tv.wire()).location.endswith(check_device_url(location, TV)[1])


def test_looking_for_a_deaf_tv_stays_on_it_and_ends_within_ten_seconds(tv: FakeTv) -> None:
    tv.datagrams, tv.off = [], True
    started = tv.now
    with pytest.raises(TvUnreachable):
        find(TV, tv.wire())
    assert tv.now - started <= 10.0
    assert len({port for _, port in tv.connects}) >= 6
    assert {address for address, _ in tv.connects} == {TV}


def test_a_silent_port_is_asked_once(tv: FakeTv) -> None:
    tv.datagrams, tv.off = [], True
    with pytest.raises(TvUnreachable):
        find(TV, tv.wire())
    assert len(tv.connects) == len(set(tv.connects))


def test_a_deaf_tv_answering_drip_by_drip_ends_within_ten_seconds(tv: FakeTv) -> None:
    tv.datagrams = []
    tv.seconds_per_read, tv.read_size = 0.3, 1
    started = tv.now
    with pytest.raises(TvUnreachable):
        find(TV, tv.wire())
    assert tv.now - started <= 10.0


def test_a_guessed_description_gets_the_same_checks(tv: FakeTv) -> None:
    tv.datagrams = []
    tv.pages = {"/dmr": http(DESCRIPTION.replace("/smp_22_", "http://10.0.0.1:7676/x"))}
    with pytest.raises(TvRefused, match="other than its own"):
        find(TV, tv.wire())


def test_the_first_refusal_among_guesses_is_the_one_told(tv: FakeTv) -> None:
    tv.datagrams = []
    tv.pages = {
        "/dmr": http(DESCRIPTION.replace("/smp_22_", f"http://{TV}:22/x")),
        "/dmr.xml": http(DESCRIPTION.replace("/smp_22_", "http://10.0.0.1:7676/x")),
    }
    with pytest.raises(TvRefused, match="port 22"):
        find(TV, tv.wire())


def test_a_refused_guess_does_not_hide_a_good_one(tv: FakeTv) -> None:
    tv.datagrams = []
    tv.pages["/dmr"] = http(DESCRIPTION.replace("/smp_22_", "http://10.0.0.1:7676/x"))
    assert find(TV, tv.wire()).location == LOCATION


def test_ports_that_each_answer_slowly_do_not_stretch_the_ten_seconds(tv: FakeTv) -> None:
    tv.datagrams, tv.pages, tv.seconds_per_read = [], {}, 0.95
    started = tv.now
    with pytest.raises(TvUnreachable):
        find(TV, tv.wire())
    assert tv.now - started <= 10.0


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
    assert tv.closed == len(tv.connects) + 1


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
        tv.wire().search(TV, lambda _url: None)
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
    tv.actions["GetVolume"] = http(body)
    with pytest.raises(TvBadAnswer):
        renderer.volume()


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


@pytest.mark.parametrize("version", ["1.0", "1.1"])
@pytest.mark.parametrize("framing", ["length", "none", "chunked"])
@pytest.mark.parametrize("connection", ["", "Connection: close\r\n"])
def test_a_tv_that_closes_after_its_answer_is_read_to_the_end(
    tv: FakeTv, version: str, framing: str, connection: str
) -> None:
    body = DESCRIPTION.encode()
    extra = ""
    if framing == "length":
        extra = f"Content-Length: {len(body)}\r\n"
    elif framing == "chunked":
        extra = "Transfer-Encoding: chunked\r\n"
        body = b"%x\r\n%b\r\n0\r\n\r\n" % (len(body), body)
    head = f"HTTP/{version} 200 OK\r\n{connection}{extra}\r\n".encode()
    tv.pages["/smp_14_"] = head + body
    tv.read_size = len(head)
    assert find(TV, tv.wire()).name == "[TV] Living room"
    assert tv.closed == len(tv.connects) + 1


@pytest.mark.parametrize("value", ["", " ", "identity", "Identity "])
def test_a_blank_content_encoding_is_none(tv: FakeTv, value: str) -> None:
    tv.pages["/smp_14_"] = http(DESCRIPTION, headers=f"Content-Encoding:{value}\r\n")
    assert find(TV, tv.wire()).address == TV


def test_a_body_without_a_length_that_ends_in_a_reset_is_read(tv: FakeTv) -> None:
    tv.pages["/smp_14_"] = b"HTTP/1.1 200 OK\r\n\r\n" + DESCRIPTION.encode()
    tv.reset_after_answer = True
    assert find(TV, tv.wire()).address == TV


@pytest.mark.parametrize(
    "head", ["Content-Length: 9999\r\n", "Transfer-Encoding: chunked\r\n", None]
)
def test_a_reset_before_the_promised_end_counts_as_gone(tv: FakeTv, head: str | None) -> None:
    body = "" if head is None else f"HTTP/1.1 200 OK\r\n{head}\r\n"
    tv.pages["/smp_14_"] = body.encode()
    tv.reset_after_answer = True
    with pytest.raises(TvUnreachable):
        describe(TV, LOCATION, tv.wire())


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (b"", b"\x00"),
        (b"", b"\r\n\x00\x00"),
        (b"\r\n ", b""),
        (b"\xef\xbb\xbf", b""),
        (b"\xef\xbb\xbf\r\n", b" \x00\n"),
    ],
)
def test_padding_around_xml_is_ignored(tv: FakeTv, before: bytes, after: bytes) -> None:
    tv.pages["/smp_14_"] = http(before + DESCRIPTION.encode() + after)
    assert find(TV, tv.wire()).address == TV


def test_utf_16_xml_keeps_its_last_byte(tv: FakeTv) -> None:
    tv.pages["/smp_14_"] = http(DESCRIPTION.encode("utf-16"))
    assert find(TV, tv.wire()).address == TV


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


def test_the_tvs_own_id_is_given_so_that_it_can_be_compared(tv: FakeTv, found: Renderer) -> None:
    assert found.udn == "uuid:1234-abcd"
    tv.pages["/smp_14_"] = http(DESCRIPTION.replace("1234-abcd", "a&#10;" + "x" * 5000))
    udn = describe(TV, LOCATION, tv.wire()).udn
    assert udn.isprintable()
    assert len(udn) <= 200


def test_a_bad_volume_address_costs_the_volume_and_nothing_else(tv: FakeTv) -> None:
    tv.pages["/smp_14_"] = http(DESCRIPTION.replace("/smp_17_", "http://10.0.0.1:7676/x"))
    renderer = find(TV, tv.wire())
    assert not renderer.has_volume
    renderer.play()


def _with_volume_range(tv: FakeTv, scpd_url: str, maximum: str = "50") -> Renderer:
    tv.pages["/smp_14_"] = http(
        DESCRIPTION.replace("<SCPDURL>/rc.xml</SCPDURL>", f"<SCPDURL>{scpd_url}</SCPDURL>")
    )
    tv.pages["/rc.xml"] = http(
        '<scpd xmlns="urn:schemas-upnp-org:service-1-0"><serviceStateTable>'
        "<stateVariable><name>Brightness</name><allowedValueRange>"
        "<maximum>7</maximum></allowedValueRange></stateVariable>"
        "<stateVariable><name>Volume</name><allowedValueRange>"
        f"<minimum>0</minimum><maximum>{maximum}</maximum></allowedValueRange></stateVariable>"
        "</serviceStateTable></scpd>"
    )
    tv.actions["GetVolume"] = soap("GetVolume", "<CurrentVolume>20</CurrentVolume>")
    return describe(TV, LOCATION, tv.wire())


def _volume_sent(tv: FakeTv) -> str | None:
    return _body(tv.requests[-1])[0][0].findtext("DesiredVolume")


def test_volume_is_scaled_to_what_the_tv_counts_to(tv: FakeTv) -> None:
    renderer = _with_volume_range(tv, "/rc.xml")
    assert renderer.volume() == 40
    renderer.set_volume(30)
    assert _volume_sent(tv) == "15"
    renderer.set_volume(500)
    assert _volume_sent(tv) == "50"


@pytest.mark.parametrize("missing", ["<maximum>50</maximum>", "50"])
def test_a_volume_range_without_a_maximum_counts_to_a_hundred(tv: FakeTv, missing: str) -> None:
    _with_volume_range(tv, "/rc.xml")
    tv.pages["/rc.xml"] = http(
        tv.pages["/rc.xml"].split(b"\r\n\r\n", 1)[1].replace(missing.encode(), b"")
    )
    assert describe(TV, LOCATION, tv.wire()).volume() == 20


@pytest.mark.parametrize("maximum", ["0", "-5", "x", "9" * 5000])
def test_a_made_up_volume_range_leaves_the_volume_out(tv: FakeTv, maximum: str) -> None:
    assert not _with_volume_range(tv, "/rc.xml", maximum).has_volume


@pytest.mark.parametrize(
    "url", ["http://10.0.0.1:7676/rc.xml", f"http://{TV}:22/rc.xml", "/gone", "/smp_14_", ""]
)
def test_a_volume_range_that_cannot_be_read_leaves_the_volume_out(tv: FakeTv, url: str) -> None:
    renderer = _with_volume_range(tv, url)
    assert not renderer.has_volume
    assert set(tv.connects) == {(TV, 7676)}
    renderer.play()


def test_a_slow_volume_range_does_not_make_describing_slower(tv: FakeTv) -> None:
    tv.seconds_per_read = 2.0
    started = tv.now
    renderer = _with_volume_range(tv, "/rc.xml")
    assert tv.now - started <= 3.0
    assert not renderer.has_volume


def test_an_error_page_is_not_a_volume_range(tv: FakeTv) -> None:
    tv.pages["/rc.xml"] = http(VOLUME_RANGE, "500 Internal Server Error")
    assert not describe(TV, LOCATION, tv.wire()).has_volume


def test_the_volume_range_is_read_once_for_the_chosen_device_alone(tv: FakeTv) -> None:
    device = DESCRIPTION[DESCRIPTION.index("<device>") : DESCRIPTION.index("</root>")]
    other = device.replace(AVT, "urn:other")
    tv.pages["/smp_14_"] = http(
        f'<root xmlns="urn:schemas-upnp-org:device-1-0">{other * 20}{device}{device * 20}</root>'
    )
    assert describe(TV, LOCATION, tv.wire()).has_volume
    assert len(tv.connects) == 2


def test_a_bad_play_address_is_refused_by_its_cause(tv: FakeTv) -> None:
    tv.pages["/smp_14_"] = http(DESCRIPTION.replace("/smp_22_", "http://10.0.0.1:7676/x"))
    with pytest.raises(TvRefused, match="other than its own"):
        find(TV, tv.wire())


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
    found.start(FILM)
    (action,) = _body(tv.requests[-2])[0]
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


COMMANDS = {
    "SetAVTransportURI": lambda r: r.start(FILM),
    "Play": lambda r: r.play(),
    "Pause": lambda r: r.pause(),
    "Stop": lambda r: r.stop(),
    "Seek": lambda r: r.seek(60),
}


def _sent(tv: FakeTv) -> list[str]:
    actions = [r.split(b"#", 1)[1].split(b'"')[0].decode() for r in tv.requests if b"#" in r]
    return actions


@pytest.mark.parametrize("action", COMMANDS)
def test_a_command_may_take_twenty_seconds_and_no_more(
    tv: FakeTv, found: Renderer, action: str
) -> None:
    tv.slow[action] = 19.0
    COMMANDS[action](found)
    tv.slow[action] = 21.0
    started = tv.now
    with pytest.raises(TvUnreachable):
        COMMANDS[action](found)
    assert tv.now - started == pytest.approx(20.0)


def test_connecting_is_given_three_seconds_whatever_the_command(
    tv: FakeTv, found: Renderer
) -> None:
    found.play()
    assert max(tv.connect_timeouts) <= 3.0


@pytest.mark.parametrize("action", ["GetTransportInfo", "GetVolume", "SetVolume"])
def test_asking_and_volume_keep_three_seconds(tv: FakeTv, found: Renderer, action: str) -> None:
    tv.slow[action] = 4.0
    ask = {
        "GetTransportInfo": found.status,
        "GetVolume": found.volume,
        "SetVolume": lambda: found.set_volume(5),
    }[action]
    with pytest.raises(TvUnreachable):
        ask()


@pytest.mark.parametrize(
    ("action", "sent"),
    [
        ("SetAVTransportURI", ["SetAVTransportURI", "Stop"]),
        ("Play", ["SetAVTransportURI", "Play", "Stop"]),
    ],
)
def test_a_tv_that_is_too_slow_to_start_is_told_once_to_stop(
    tv: FakeTv, found: Renderer, action: str, sent: list[str]
) -> None:
    tv.slow = {action: 60.0, "Stop": 60.0}
    started = tv.now
    with pytest.raises(TvUnreachable):
        found.start(FILM)
    assert _sent(tv) == sent
    assert tv.now - started <= 23.0


def test_a_tv_that_never_got_the_request_is_not_told_to_stop(tv: FakeTv, found: Renderer) -> None:
    connects = len(tv.connects)
    tv.off = True
    with pytest.raises(TvUnreachable):
        found.start(FILM)
    assert len(tv.connects) == connects + 1


@pytest.mark.parametrize("action", ["Play", "Pause", "Seek"])
def test_a_slow_command_on_a_loaded_film_does_not_stop_it(
    tv: FakeTv, found: Renderer, action: str
) -> None:
    tv.slow[action] = 60.0
    with pytest.raises(TvUnreachable):
        COMMANDS[action](found)
    assert _sent(tv) == [action]


def test_a_refused_start_is_not_followed_by_a_stop(tv: FakeTv, found: Renderer) -> None:
    tv.actions["Play"] = fault("701")
    with pytest.raises(TvFault):
        found.start(FILM)
    assert _sent(tv) == ["SetAVTransportURI", "Play"]


OK = b"HTTP/1.1 200 OK\r\n"


@pytest.mark.parametrize(
    "answer",
    [
        OK + b"Content-Length: 500\r\n\r\nshort",
        OK + b"Transfer-Encoding: chunked\r\n\r\n500\r\ncut",
        OK + b"Transfer-Encoding: chunked\r\n\r\nzz\r\n",
        OK + b"Content-Encoding: gzip\r\nContent-Length: 2\r\n\r\n\x1f\x8b",
        OK + b"\r\nno length",
        OK + b"\r\n",
        OK + b"\r\n" + b"x" * 500_000,
        b"HTTP/1.1 204 No Content\r\n\r\n",
        b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n",
    ],
)
@pytest.mark.parametrize("ending", ["close", "reset", "hold"])
@pytest.mark.parametrize("action", ["SetAVTransportURI", "Play", "SetVolume"])
def test_a_command_that_got_a_success_is_done_whatever_its_body(
    tv: FakeTv, found: Renderer, action: str, ending: str, answer: bytes
) -> None:
    tv.actions[action] = answer
    tv.reset_after_answer = ending == "reset"
    tv.hold_after_answer = ending == "hold"
    started = tv.now
    if action == "SetVolume":
        found.set_volume(5)
    else:
        found.start(FILM)
        assert _sent(tv) == ["SetAVTransportURI", "Play"]
    assert tv.now - started <= 3.0
    assert tv.bytes_read < 200_000


@pytest.mark.parametrize("status", [b"200 OK", b"500 Internal Server Error"])
@pytest.mark.parametrize("ending", ["close", "reset", "hold"])
def test_a_fault_that_can_be_read_counts_however_the_answer_ends(
    tv: FakeTv, found: Renderer, status: bytes, ending: str
) -> None:
    body = fault("704").split(b"\r\n\r\n", 1)[1]
    tv.actions["Play"] = b"HTTP/1.1 " + status + b"\r\n\r\n" + body
    tv.reset_after_answer = ending == "reset"
    tv.hold_after_answer = ending == "hold"
    with pytest.raises(TvFault, match="kind of video"):
        found.start(FILM)
    assert _sent(tv) == ["SetAVTransportURI", "Play"]


def test_a_success_at_the_last_moment_is_still_a_success(tv: FakeTv, found: Renderer) -> None:
    tv.slow["Play"] = 19.9
    tv.actions["Play"] = OK + b"\r\n"
    tv.hold_after_answer = True
    started = tv.now
    found.play()
    assert tv.now - started <= 20.0 + 1e-6


def test_a_reset_inside_the_headers_is_not_a_success(tv: FakeTv, found: Renderer) -> None:
    tv.actions["Play"] = OK + b"Content-Type: text/xml\r\n"
    tv.reset_after_answer = True
    with pytest.raises(TvUnreachable):
        found.play()


@pytest.mark.parametrize("body", ["", "OK", "<root/>", "<a>"])
@pytest.mark.parametrize("action", [*COMMANDS, "SetVolume"])
def test_any_success_answer_will_do_for_a_command(
    tv: FakeTv, found: Renderer, action: str, body: str
) -> None:
    tv.actions[action] = http(body)
    if action == "SetVolume":
        found.set_volume(5)
    else:
        COMMANDS[action](found)


def test_a_fault_sent_as_a_success_is_still_a_fault(tv: FakeTv, found: Renderer) -> None:
    tv.actions["Play"] = fault("704").replace(b"500 Internal Server Error", b"200 OK")
    with pytest.raises(TvFault, match="kind of video"):
        found.play()


@pytest.mark.parametrize("seconds", [math.nan, math.inf])
def test_a_jump_to_no_real_time_is_refused(tv: FakeTv, found: Renderer, seconds: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        found.seek(seconds)  # type: ignore[arg-type]
    assert _sent(tv) == []


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
    found.start(FILM)
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
    tv.datagrams, tv.off = [], True
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


def test_a_part_of_a_second_is_left_out() -> None:
    media = Media(url="http://a/f.ts", title="x", seconds=62.9)  # type: ignore[arg-type]
    assert 'duration="0:01:02.000"' in media.didl()


@pytest.mark.parametrize("seconds", [-1, math.nan, math.inf, -math.inf])
def test_a_film_of_no_real_length_is_refused(seconds: float) -> None:
    with pytest.raises(ValueError, match="real length"):
        Media(url="http://a/f.ts", title="x", seconds=seconds)  # type: ignore[arg-type]


@pytest.mark.parametrize("size", ['1" x="', "1", -1, 1.5, True])
def test_a_film_of_no_real_size_is_refused(size: object) -> None:
    with pytest.raises(ValueError, match="real size"):
        Media(url="http://a/f.ts", title="x", size=size)  # type: ignore[arg-type]
    assert ' size="7"' in Media(url="http://a/f.ts", title="x", size=7).didl()


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


def test_a_picture_is_told_as_one() -> None:
    picture = Media(url="http://a/p.jpg", title="x", mime="image/jpeg", converted=False)
    assert "<upnp:class>object.item.imageItem.photo</upnp:class>" in picture.didl()
    assert "http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_SM;DLNA.ORG_OP=00;" in picture.didl()
    headers = picture.stream_headers({"getcontentFeatures.dlna.org": "1"})
    assert headers["transferMode.dlna.org"] == "Interactive"
    assert "DLNA.ORG_CI=0" in headers["contentFeatures.dlna.org"]
    assert "object.item.videoItem" in FILM.didl()


# --- a TV that moved -------------------------------------------------------

MOVED = "192.168.1.77"


def test_a_tv_that_moved_is_found_by_its_own_id() -> None:
    tv = MovedTv(MOVED)
    found = find_again("uuid:1234-abcd", TV, tv.wire())
    assert (found.address, found.udn) == (MOVED, "uuid:1234-abcd")
    assert {address for address, _ in tv.connects} == {MOVED}
    assert all(b"ST: uuid:1234-abcd\r\n" in data for data, _ in tv.sent_udp)


def test_a_moved_tv_is_looked_for_only_around_where_it_was_and_for_three_seconds() -> None:
    tv = MovedTv("192.168.2.20")
    with pytest.raises(TvUnreachable):
        find_again("uuid:1234-abcd", TV, tv.wire(), skip={"192.168.1.10"})
    asked = {address for _, (address, _) in tv.sent_udp}
    assert asked == {f"192.168.1.{n}" for n in range(1, 255)} - {"192.168.1.10"}
    assert len(tv.sent_udp) == 3 * len(asked)
    assert tv.now - 1000 <= 3.01


def test_mediamans_own_network_is_left_out_of_the_search() -> None:
    tv = MovedTv(MOVED)
    tv.own = [ipaddress.IPv4Network("192.168.1.64/26")]
    with pytest.raises(TvUnreachable):
        find_again("uuid:1234-abcd", TV, tv.wire())
    assert not any(address == MOVED for _, (address, _) in tv.sent_udp)


def test_an_answer_for_another_device_is_not_the_tv() -> None:
    tv = MovedTv(MOVED)
    tv.usn = "uuid:9999"
    with pytest.raises(TvUnreachable):
        find_again("uuid:1234-abcd", TV, tv.wire())
    assert tv.connects == []


def test_a_device_that_only_says_it_is_the_tv_is_refused() -> None:
    tv = MovedTv(MOVED)
    tv.pages["/smp_14_"] = http(DESCRIPTION.replace("uuid:1234-abcd", "uuid:9999"))
    with pytest.raises(AnotherTv):
        find_again("uuid:1234-abcd", TV, tv.wire())


def test_an_id_that_could_forge_a_search_is_not_sent() -> None:
    tv = MovedTv(MOVED)
    with pytest.raises(TvUnreachable):
        find_again("uuid:1\r\nST: ssdp:all", TV, tv.wire())
    assert tv.sent_udp == []


# --- real sockets, on this machine only ------------------------------------


def test_the_real_connection_sends_and_reads() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        server.settimeout(2)
        client = _open_tcp("127.0.0.1", server.getsockname()[1], 2.0)
        peer, _ = server.accept()
        with peer:
            peer.settimeout(2)
            client.sendall(b"ask")
            assert peer.recv(16) == b"ask"
            peer.sendall(b"answer")
            buffer = bytearray(16)
            assert buffer[: client.recv_into(buffer)] == b"answer"
        client.close()


def test_a_connection_is_given_a_limit_and_closed_when_it_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[socket.socket] = []

    timeouts: list[float | None] = []

    class Recorded(socket.socket):
        def __init__(self, *args: int) -> None:
            super().__init__(*args)
            opened.append(self)

        def connect(self, address: tuple[str, int]) -> None:
            timeouts.append(self.gettimeout())
            super().connect(address)

    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
    monkeypatch.setattr(socket, "socket", Recorded)
    with pytest.raises(ConnectionRefusedError):
        _open_tcp("127.0.0.1", port, 2.0)
    assert [sock.fileno() for sock in opened] == [-1]
    assert timeouts == [2.0]


def test_the_real_datagram_socket_asks_and_hears_the_sender() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
        peer.bind(("127.0.0.1", 0))
        peer.settimeout(2)
        client = _open_udp()
        client.settimeout(2)
        client.sendto(b"ask", peer.getsockname())
        data, sender = peer.recvfrom(16)
        assert data == b"ask"
        peer.sendto(b"answer", sender)
        assert client.recvfrom(16) == (b"answer", peer.getsockname())
        client.close()
