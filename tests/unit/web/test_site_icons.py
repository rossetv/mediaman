"""Every page names mediaman's icons, at the running version, and each is served as what it says it is."""

from __future__ import annotations

import re
import zlib
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from mediaman.app_factory import create_app


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


@pytest.mark.parametrize(
    ("rel", "kind"),
    [("icon", "image/svg+xml"), ("icon", "image/png"), ("apple-touch-icon", "image/png")],
)
def test_a_page_links_an_icon_that_is_there(client, rel, kind):
    page = client.get("/login").text
    links = re.findall(rf'<link rel="{rel}"[^>]* href="([^"]+)"', page)
    version = client.app.state.templates.env.globals["static_v"]

    assert links
    assert all(link.endswith(f"?v={version}") for link in links)
    answers = [client.get(link) for link in links]
    assert {a.status_code for a in answers} == {200}
    assert kind in {a.headers["content-type"] for a in answers}


def test_the_browser_frame_takes_the_page_colour(client):
    css = client.get("/static/css/_tokens.css").text
    background = re.search(r"--bg:\s*(#\w+);", css).group(1)

    assert f'<meta name="theme-color" content="{background}">' in client.get("/login").text


def _pixels(png: bytes) -> tuple[int, list[list[tuple[int, ...]]]]:
    """The width of a square 8-bit picture and its rows, each pixel as red, green, blue, alpha."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    at, data, size, parts = 8, b"", 0, 0
    while at < len(png):
        length = int.from_bytes(png[at : at + 4], "big")
        kind, body = png[at + 4 : at + 8], png[at + 8 : at + 8 + length]
        if kind == b"IHDR":
            size, parts = int.from_bytes(body[:4], "big"), {2: 3, 6: 4}[body[9]]
            assert int.from_bytes(body[4:8], "big") == size
        data += body if kind == b"IDAT" else b""
        at += length + 12
    raw, before, rows = zlib.decompress(data), bytes(size * parts), []
    for y in range(size):
        start = y * (size * parts + 1)
        line = bytearray(raw[start + 1 : start + 1 + size * parts])
        for x in range(len(line)):
            left = line[x - parts] if x >= parts else 0
            up, corner = before[x], before[x - parts] if x >= parts else 0
            near = min((left, up, corner), key=lambda v: abs(left + up - corner - v))
            line[x] = (line[x] + (0, left, up, (left + up) // 2, near)[raw[start]]) % 256
        rows.append([(*line[i : i + parts], 255)[:4] for i in range(0, len(line), parts)])
        before = bytes(line)
    return size, rows


_TILE, _WHITE, _BLUE, _CLEAR = (29, 29, 31, 255), (255,) * 3 + (255,), (41, 151, 255, 255), (0,) * 4


@pytest.mark.parametrize(
    ("name", "size", "mark", "corner"),
    [("favicon.png", 48, 1, _CLEAR), ("apple-touch-icon.png", 180, 0.86, _TILE)],
)
def test_a_picture_of_the_icon_has_its_size_and_shows_the_mark(client, name, size, mark, corner):
    width, rows = _pixels(client.get(f"/static/icons/{name}").content)

    def at(x, y):
        # A place in the drawing, which is 64 wide and shrunk round its middle.
        return rows[int((32 + (y - 32) * mark) * size / 64)][
            int((32 + (x - 32) * mark) * size / 64)
        ]

    assert width == size
    assert rows[0][0] == corner
    assert at(12.5, 40) == at(32, 40) == _WHITE
    assert at(51.5, 40) == _BLUE
    # Over, inside and under the two arches, which end 15.5 above the foot of the tile.
    assert at(32, 9) == at(22, 40) == at(41.7, 40) == _TILE
    assert at(12.5, 50.5) == at(32, 50.5) == at(51.5, 50.5) == _TILE


def test_the_drawn_icon_has_one_mark_for_a_tab_and_one_for_larger_places(client):
    root = ElementTree.fromstring(client.get("/static/icons/favicon.svg").text)
    tag = "{http://www.w3.org/2000/svg}"
    groups = {group.get("class"): group for group in root.iter(f"{tag}g")}

    assert set(groups) == {"small", "large"}
    for group in groups.values():
        assert group.find(f"{tag}rect").get("fill") == "#1d1d1f"
        # The white arches stand in front of the blue one.
        assert [path.get("stroke") for path in group.iter(f"{tag}path")] == ["#2997ff", "#fff"]
    style = root.find(f"{tag}style").text
    assert style.startswith(".small{display:none}@media (max-width:24px){")
    assert style.endswith(".small{display:inline}.large{display:none}}")
    large = groups["large"].findall(f"{tag}path")[1].get("d")
    assert large == "M12.5 43.75V30.5a9.75 9.75 0 0 1 19.5 0V43.75"
