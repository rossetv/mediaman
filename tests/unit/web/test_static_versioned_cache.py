"""A static file asked for by the running version is kept for good; any other ask is checked again."""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.app_factory import _RevalidatedStaticFiles, create_app

_FILE = "/static/css/_base.css"


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


@pytest.fixture(scope="module")
def version(client):
    return client.app.state.templates.env.globals["static_v"]


def test_a_file_at_the_running_version_is_kept_for_a_year(client, version):
    fresh = client.get(f"{_FILE}?v={version}")
    again = client.get(f"{_FILE}?v={version}", headers={"If-None-Match": fresh.headers["ETag"]})

    assert (fresh.status_code, again.status_code) == (200, 304)
    for resp in (fresh, again):
        assert resp.headers["Cache-Control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize(
    "query",
    ["", "?v=", "?v=000000000000", "?v={v}0", "?v={v}&v=000000000000", "?x={v}"],
)
def test_any_other_ask_is_checked_again_on_every_load(client, version, query):
    resp = client.get(_FILE + query.format(v=version))

    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-cache"


def test_a_missing_file_at_the_running_version_is_not_kept(client, version):
    resp = client.get(f"/static/css/none.css?v={version}")

    assert resp.status_code == 404
    assert "immutable" not in resp.headers.get("Cache-Control", "")


def test_a_file_changed_since_the_version_was_read_is_checked_again(tmp_path):
    sheet = tmp_path / "a.css"
    sheet.write_text("a{}")
    os.utime(sheet, (0, 0))
    app = FastAPI()
    app.mount("/static", _RevalidatedStaticFiles(directory=str(tmp_path), version="abc"))
    client = TestClient(app)
    assert "immutable" in client.get("/static/a.css?v=abc").headers["Cache-Control"]

    later = 32503680000
    os.utime(sheet, (later, later))

    assert client.get("/static/a.css?v=abc").headers["Cache-Control"] == "no-cache"
