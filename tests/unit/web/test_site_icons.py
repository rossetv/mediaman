"""Every page names mediaman's icons, at the running version, and each is served as what it says it is."""

from __future__ import annotations

import re

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
