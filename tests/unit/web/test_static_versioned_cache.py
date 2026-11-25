"""A static file asked for by the running version is kept for good; any other ask is checked again."""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman import app_factory
from mediaman.app_factory import _RevalidatedStaticFiles, _static_version, create_app

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


@pytest.mark.parametrize("accept", ["*/*", "text/html"])
def test_a_missing_file_at_the_running_version_is_not_kept(client, version, accept):
    resp = client.get(f"/static/css/none.css?v={version}", headers={"Accept": accept})

    assert resp.status_code == 404
    assert resp.headers["Cache-Control"] == "no-cache"


def test_a_refused_method_at_the_running_version_is_not_kept(client, version):
    resp = client.post(f"{_FILE}?v={version}")

    assert resp.status_code == 405
    assert resp.headers["Cache-Control"] == "no-cache"


@pytest.fixture
def sheet(tmp_path):
    path = tmp_path / "a.css"
    path.write_text("a{}")
    os.utime(path, (1000, 1000))
    return path


def _serve(sheet):
    """``get()``: the Cache-Control of *sheet* at the version read now."""
    version = _static_version(sheet.parent)
    app = FastAPI()
    app.mount("/static", _RevalidatedStaticFiles(directory=str(sheet.parent), version=version))
    client = TestClient(app)
    return lambda: client.get(f"/static/a.css?v={version.digest}").headers["Cache-Control"]


def test_a_file_changed_since_the_version_was_read_is_checked_again(sheet):
    cache_control = _serve(sheet)
    assert "immutable" in cache_control()

    later = 32503680000
    os.utime(sheet, (later, later))

    assert cache_control() == "no-cache"


@pytest.mark.parametrize(("text", "mtime"), [("b{}", 999), ("a{color:red}", 1000)])
def test_a_file_replaced_by_one_no_newer_is_checked_again(sheet, text, mtime):
    cache_control = _serve(sheet)

    sheet.write_text(text)
    os.utime(sheet, (mtime, mtime))

    assert cache_control() == "no-cache"


def test_a_file_that_appeared_since_the_version_was_read_is_checked_again(sheet):
    version = _static_version(sheet.parent)
    sheet.rename(sheet.with_name("b.css"))
    app = FastAPI()
    app.mount("/static", _RevalidatedStaticFiles(directory=str(sheet.parent), version=version))

    resp = TestClient(app).get(f"/static/b.css?v={version.digest}")

    assert resp.headers["Cache-Control"] == "no-cache"


def test_a_file_written_while_the_version_was_being_read_is_checked_again(sheet, monkeypatch):
    class Clock:
        now = 999.0

        def time(self):
            return self.now

    clock = Clock()
    monkeypatch.setattr(app_factory, "time", clock)
    read_bytes = type(sheet).read_bytes

    def slow_read(path):
        clock.now = 1001.0
        return read_bytes(path)

    monkeypatch.setattr(type(sheet), "read_bytes", slow_read)

    assert _serve(sheet)() == "no-cache"
