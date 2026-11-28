"""The Activity tab's endpoints: admins only, the server now and its history by period."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from mediaman.services.activity import history
from mediaman.services.activity.sampler import Sampler
from mediaman.web.models.settings import SettingsUpdate
from mediaman.web.routes.activity import router


@pytest.fixture(autouse=True)
def fresh_sampler(monkeypatch):
    monkeypatch.setattr("mediaman.web.routes.activity.SAMPLER", Sampler())


@pytest.mark.parametrize("url", ["/api/activity", "/api/activity/history?period=1h"])
def test_only_admins_are_answered(url, app_factory, viewer_client, conn):
    app = app_factory(router, conn=conn)
    assert TestClient(app).get(url).status_code == 401
    assert viewer_client(app, conn).get(url).status_code == 403


def test_the_server_now_says_which_sources_are_set_up_and_what_mediaman_is_busy_with(
    app_factory, authed_client, conn
):
    conn.executemany(
        "INSERT INTO arr_searching_since (key, since) VALUES (?, '2026-11-28T10:00:00+00:00')",
        [("radarr:4",), ("sonarr:9:1",), ("sonarr:9:2",), ("sonarr:12",)],
    )
    conn.commit()
    body = authed_client(app_factory(router, conn=conn), conn).get("/api/activity").json()
    assert body["sources"] == {"plex": "off", "nzbget": "off", "docker": "off"}
    assert body["work"]["searching"] == {"radarr": 1, "sonarr": 2}  # a show's seasons are one title
    assert body["point"]["apps"]["mediaman"][1] > 0
    assert body["live"][-1]["at"] == body["point"]["at"]


def test_a_tab_just_opened_gets_the_last_minute_from_the_history(app_factory, authed_client, conn):
    now = time.time()
    history.store(conn, {"down": 5.0}, now - 40)
    history.store(conn, {"down": 6.0}, now - 20)
    history.store(conn, {"down": 7.0}, now - 600)  # older than the minute
    body = authed_client(app_factory(router, conn=conn), conn).get("/api/activity").json()
    assert [p["down"] for p in body["live"][:-1]] == [5.0, 6.0]


def test_a_period_answers_its_points_and_an_unknown_one_is_refused(
    app_factory, authed_client, conn
):
    now = time.time()
    history.store(conn, {"down": 5.0, "cpu": 40.0}, now - 30)
    client = authed_client(app_factory(router, conn=conn), conn)
    got = client.get("/api/activity/history?period=1h").json()
    assert [p["cpu"] for p in got["points"]] == [40.0]
    assert got["since"] == int(now - 30)
    assert client.get("/api/activity/history?period=2y").status_code == 400


def test_the_docker_proxy_setting_must_be_a_web_address():
    assert SettingsUpdate(docker_proxy_url="http://docker-proxy:2375").docker_proxy_url
    with pytest.raises(ValueError):
        SettingsUpdate(docker_proxy_url="unix:///var/run/docker.sock")
