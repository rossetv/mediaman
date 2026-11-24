"""Every /static answer is revalidated, so a browser cannot run old assets after an update."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.app_factory import _STATIC_DIR, _RevalidatedStaticFiles, _static_version
from mediaman.web import register_security_middleware

_KINDS = {
    "css": "css/_base.css",
    "js": "js/nav.js",
    "font": "fonts/fontawesome/webfonts/fa-solid-900.woff2",
    "vendor": "vendor/hls/LICENSE",
}


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_security_middleware(app)
    app.mount("/static", _RevalidatedStaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/page")
    def _page() -> dict:
        return {}

    return TestClient(app)


@pytest.mark.parametrize("path", _KINDS.values(), ids=_KINDS.keys())
def test_static_file_answers_no_cache_on_200_and_304(client, path):
    first = client.get(f"/static/{path}")
    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "no-cache"

    again = client.get(f"/static/{path}", headers={"If-None-Match": first.headers["ETag"]})
    assert again.status_code == 304
    assert again.headers["Cache-Control"] == "no-cache"
    assert again.headers["ETag"] == first.headers["ETag"]


def test_other_responses_keep_their_cache_control(client):
    assert client.get("/page").headers["Cache-Control"] == "no-store, private"


_TEMPLATES = Path(__file__).resolve().parents[3] / "src/mediaman/web/templates"
_ASSET_LINK = re.compile(r"""(?:href|src)=["'](/static/[^"']*\.(?:css|js))["']""")


def test_no_template_links_css_or_js_without_a_version():
    bare = [
        f"{page.name}: {m.group(1)}"
        for page in _TEMPLATES.rglob("*.html")
        for m in _ASSET_LINK.finditer(page.read_text())
    ]
    assert bare == []


def test_static_version_changes_with_a_file_and_stays_put_otherwise(tmp_path):
    (tmp_path / "a.css").write_text("a{}")
    first = _static_version(tmp_path)
    assert _static_version(tmp_path) == first
    (tmp_path / "a.css").write_text("a{color:red}")
    assert _static_version(tmp_path) != first


def test_pages_get_the_version_from_the_app(guarded_app):
    app, _ = guarded_app
    env = app.state.templates.env
    html = env.from_string('<link href="/static/css/x.css?v={{ static_v }}">').render()
    assert re.search(r"x\.css\?v=[0-9a-f]{12}\"", html)
