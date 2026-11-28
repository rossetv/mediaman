"""The Activity tab's endpoints: admins only, the server now and its history by period."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from mediaman.services.activity import history
from mediaman.services.activity.sampler import Sampler
from mediaman.web.models.settings import SettingsUpdate
from mediaman.web.routes.activity import router
from mediaman.web.routes.settings.core import validate_url_fields
from mediaman.web.routes.settings.testers import test_docker as docker_tester
from tests.unit.services.activity.fakes import DOCKER, Stack, configure


@pytest.fixture(autouse=True)
def fresh_sampler(monkeypatch):
    # Healthy fakes answer at once, so a long budget costs nothing and a busy machine passes.
    monkeypatch.setattr("mediaman.web.routes.activity.SAMPLER", Sampler(budget=5.0))


@pytest.fixture
def stack(fake_http, fake_response):
    s = Stack(fake_response)
    fake_http.handler(s)
    yield s
    s.released.set()


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


def test_a_source_that_hangs_does_not_hold_the_answer_past_the_budget(
    app_factory, authed_client, conn, secret_key, stack, monkeypatch
):
    monkeypatch.setattr("mediaman.web.routes.activity.SAMPLER", Sampler(budget=1.0))
    configure(conn, secret_key)
    stack.hang.add("nzbget")
    client = authed_client(app_factory(router, conn=conn), conn)
    started = time.monotonic()
    body = client.get("/api/activity").json()
    assert time.monotonic() - started < 3.0  # the budget is 1 s; NZBGet would take 10
    assert body["sources"] == {"plex": "ok", "nzbget": "down", "docker": "ok"}


def test_plex_without_its_resource_figures_is_down_not_an_error(
    app_factory, authed_client, conn, secret_key, stack, fake_response
):
    configure(conn, secret_key)
    stack.odd["plex"] = fake_response(status=404)
    resp = authed_client(app_factory(router, conn=conn), conn).get("/api/activity")
    assert resp.status_code == 200
    assert resp.json()["sources"]["plex"] == "down"


@pytest.mark.parametrize("url", [DOCKER + "/?x=", DOCKER + "#y", DOCKER + "/dock?"])
def test_a_docker_proxy_address_with_a_query_or_fragment_is_refused(url):
    resp = validate_url_fields({"docker_proxy_url": url})
    assert resp is not None and resp.status_code == 400


def test_the_docker_test_calls_a_reply_that_is_not_json_unrecognised(stack, fake_response):
    stack.odd["docker"] = fake_response(text="<html>a web page</html>")
    body = json.loads(docker_tester({"docker_proxy_url": DOCKER}).body)
    assert body["error"] == "bad_reply"


def test_the_servers_size_is_a_number_in_range_or_empty_to_unset_it():
    assert SettingsUpdate(server_memory_gb=" 16 ", server_cores="10").model_dump(
        include={"server_memory_gb", "server_cores"}
    ) == {"server_memory_gb": "16", "server_cores": "10"}
    assert SettingsUpdate(server_memory_gb="13.7").server_memory_gb == "13.7"
    assert SettingsUpdate(server_cores="").server_cores == ""
    for bad in ({"server_cores": "2.5"}, {"server_cores": "0"}, {"server_memory_gb": "lots"}):
        with pytest.raises(ValueError):
            SettingsUpdate(**bad)


def test_the_servers_size_and_cores_are_shown_with_the_reading(
    app_factory, authed_client, conn, secret_key, stack
):
    from mediaman.web.repository.settings import write_settings

    size = {"server_memory_gb": "16", "server_cores": "10"}
    write_settings(conn, body_dict=size, allowed_keys=list(size), secret_key=secret_key, now="x")
    body = authed_client(app_factory(router, conn=conn), conn).get("/api/activity").json()
    assert (body["cores"], body["mem_total"]) == (10, 16 * 2**30)
