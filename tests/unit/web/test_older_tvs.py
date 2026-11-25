"""Tests for older TVs added by address: what an admin may ask, and what is kept of the answer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session
from mediaman.web.repository import older_tvs as repo
from mediaman.web.repository import tv as paired
from mediaman.web.routes import older_tvs
from mediaman.web.routes.settings import router as settings_router
from tests.helpers.factories import insert_settings
from tests.unit.services.dlna.fake_tv import DESCRIPTION, TV, FakeTv, http

URL = "/api/settings/older-tvs"


@pytest.fixture
def tv(monkeypatch) -> FakeTv:
    fake = FakeTv()
    monkeypatch.setattr(older_tvs, "Wire", fake.wire)
    return fake


@pytest.fixture
def app(app_factory, conn, tv):
    return app_factory(older_tvs.router, settings_router, conn=conn)


@pytest.fixture
def client(app, authed_client, conn):
    insert_settings(conn, older_tvs_enabled="true")
    return authed_client(app, conn, with_reauth=True)


def _add(client, name="Kitchen TV"):
    return client.post(URL, json={"address": TV, "name": name})


def _asked(tv: FakeTv) -> bool:
    return bool(tv.sent_udp or tv.connects)


def test_a_check_says_what_the_tv_calls_itself_and_keeps_nothing(client, tv) -> None:
    got = client.post(f"{URL}/check", json={"address": TV})
    assert got.status_code == 200
    assert got.json() == {"ok": True, "name": "[TV] Living room", "address": TV}
    assert client.get(URL).json()["tvs"] == []


def test_no_tv_is_contacted_while_older_tvs_are_off(client, tv, conn) -> None:
    assert _add(client).status_code == 200
    (kept,) = repo.every(conn)
    tv.sent_udp.clear()
    tv.connects.clear()
    conn.execute("DELETE FROM settings WHERE key = 'older_tvs_enabled'")
    conn.commit()
    for url, body in (
        (f"{URL}/check", {"address": TV}),
        (URL, {"address": "192.168.1.21", "name": "x"}),
        (f"{URL}/{kept.id}/check", {}),
    ):
        got = client.post(url, json=body)
        assert (got.status_code, got.json()["error"]) == (409, "off")
    assert not _asked(tv)


@pytest.mark.parametrize(
    "address", ["8.8.8.8", "127.0.0.1", "169.254.169.254", "tv.example.com", "192.168.1.20:80", ""]
)
def test_only_a_home_address_is_asked(client, tv, address) -> None:
    got = client.post(URL, json={"address": address, "name": "x"})
    assert (got.status_code, got.json()["error"]) == (422, "bad_address")
    assert not _asked(tv)


def test_an_address_on_the_installs_own_network_is_refused_apart(client, tv) -> None:
    got = client.post(f"{URL}/check", json={"address": "172.17.0.9"})
    assert (got.status_code, got.json()["error"]) == (409, "own_network")
    assert not _asked(tv)


@pytest.mark.parametrize("key", ["tv_address", "base_url"])
def test_mediamans_own_address_is_not_asked(client, tv, conn, key) -> None:
    insert_settings(conn, **{key: f"http://{TV}:8282"})
    got = client.post(f"{URL}/check", json={"address": TV})
    assert (got.status_code, got.json()["error"]) == (422, "own_address")
    assert not _asked(tv)


def test_a_tv_that_is_off_and_a_device_that_is_no_tv_are_told_apart(client, tv) -> None:
    tv.pages["/smp_14_"] = http("<root/>")
    got = client.post(f"{URL}/check", json={"address": TV})
    assert (got.status_code, got.json()["error"]) == (502, "not_a_tv")
    tv.off, tv.datagrams = True, []
    got = client.post(f"{URL}/check", json={"address": TV})
    assert (got.status_code, got.json()["error"]) == (502, "unreachable")


def test_only_a_tv_that_answers_is_added(client, tv, conn) -> None:
    tv.off, tv.datagrams = True, []
    assert _add(client).status_code == 502
    assert repo.every(conn) == []


def test_an_added_tv_is_everyones_and_on_the_record(client, conn) -> None:
    added = _add(client).json()["tv"]
    assert (added["name"], added["address"]) == ("Kitchen TV", TV)
    assert added["last_seen_at"]
    row = paired.find(conn, added["id"])
    assert row is not None and (row.kind, row.owner, row.token_hash) == ("older", None, None)
    (kept,) = repo.every(conn)
    assert kept.udn == "uuid:1234-abcd"
    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log")]
    assert "sec:tv.added" in actions


def test_a_tv_is_added_once_though_its_address_changed(client, tv, conn) -> None:
    assert _add(client).status_code == 200
    again = _add(client, "Other")
    assert (again.status_code, again.json()["error"]) == (409, "already_added")
    conn.execute("UPDATE tvs SET address = '192.168.1.99'")
    conn.commit()
    again = client.post(f"{URL}/check", json={"address": TV})
    assert (again.status_code, again.json()["error"]) == (409, "already_added")


def test_tvs_that_say_nothing_of_themselves_are_not_one_tv(client, tv, conn) -> None:
    tv.pages["/smp_14_"] = http(DESCRIPTION.replace("uuid:1234-abcd", ""))
    assert _add(client).status_code == 200
    conn.execute("UPDATE tvs SET address = '192.168.1.99'")
    conn.commit()
    assert _add(client, "Other").status_code == 200


def test_older_tvs_are_limited_in_number(client, conn, monkeypatch) -> None:
    monkeypatch.setattr(repo, "MAX_OLDER_TVS", 0)
    got = _add(client)
    assert (got.status_code, got.json()["error"]) == (409, "too_many_tvs")


def test_a_check_of_a_kept_tv_tells_another_device_from_it(client, tv, conn) -> None:
    tv_id = _add(client).json()["tv"]["id"]
    conn.execute("UPDATE tvs SET last_seen_at = '2026-01-01T00:00:00+00:00'")
    conn.commit()
    tv.pages["/smp_14_"] = http(DESCRIPTION.replace("1234-abcd", "9999"))
    got = client.post(f"{URL}/{tv_id}/check")
    assert (got.status_code, got.json()["error"]) == (409, "another_tv")
    assert repo.every(conn)[0].last_seen_at == "2026-01-01T00:00:00+00:00"
    tv.pages["/smp_14_"] = http(DESCRIPTION)
    got = client.post(f"{URL}/{tv_id}/check")
    assert got.json()["tv"]["last_seen_at"] > "2026-01-02"


def test_a_renamed_tv_has_a_name_people_can_read(client) -> None:
    tv_id = _add(client).json()["tv"]["id"]
    assert client.patch(f"{URL}/{tv_id}", json={"name": " Attic  TV "}).status_code == 200
    assert client.get(URL).json()["tvs"][0]["name"] == "Attic TV"
    for name in ("", "   ", "a‮b", "x" * 41):
        assert client.patch(f"{URL}/{tv_id}", json={"name": name}).status_code == 422


def test_a_removed_tv_is_gone_and_on_the_record(client, conn) -> None:
    tv_id = _add(client).json()["tv"]["id"]
    assert client.delete(f"{URL}/{tv_id}").status_code == 200
    assert client.get(URL).json()["tvs"] == []
    assert client.delete(f"{URL}/{tv_id}").status_code == 404
    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log")]
    assert actions.count("sec:tv.removed") == 1


def test_a_paired_tv_is_none_of_these(client, conn) -> None:
    conn.execute(
        "INSERT INTO tvs (id, owner_id, kind, name, token_hash, created_at)"
        " SELECT 7, id, 'page', 'Living room', 'h', '2026-01-01' FROM admin_users LIMIT 1"
    )
    conn.commit()
    assert client.get(URL).json()["tvs"] == []
    assert client.patch(f"{URL}/7", json={"name": "x"}).status_code == 404
    assert client.delete(f"{URL}/7").status_code == 404
    assert client.post(f"{URL}/7/check").status_code == 404
    row = paired.find(conn, 7)
    assert row is not None and row.name == "Living room"


def test_a_tv_is_not_asked_while_two_are(client, tv) -> None:
    with older_tvs._looking(), older_tvs._looking():
        got = client.post(f"{URL}/check", json={"address": TV})
        assert (got.status_code, got.json()["error"]) == (503, "busy")
        assert not _asked(tv)
    assert client.post(f"{URL}/check", json={"address": TV}).status_code == 200


def test_checks_are_limited(client, tv) -> None:
    answers = [
        client.post(f"{URL}/check", json={"address": "8.8.8.8"}).status_code for _ in range(21)
    ]
    assert answers == [422] * 20 + [429]


def test_a_viewer_reaches_none_of_it(app, client, conn) -> None:
    tv_id = _add(client).json()["tv"]["id"]
    create_user(conn, "amy", "a-long-enough-Passw0rd!", role="viewer", enforce_policy=False)
    viewer = TestClient(app)
    viewer.cookies.set("session_token", create_session(conn, "amy"))
    for method, url, body in (
        ("GET", URL, None),
        ("POST", URL, {"address": TV, "name": "x"}),
        ("POST", f"{URL}/check", {"address": TV}),
        ("POST", f"{URL}/{tv_id}/check", {}),
        ("PATCH", f"{URL}/{tv_id}", {"name": "x"}),
        ("DELETE", f"{URL}/{tv_id}", None),
    ):
        assert viewer.request(method, url, json=body).status_code in (401, 403), (method, url)
    assert repo.every(conn)[0].name == "Kitchen TV"


# --- the settings ----------------------------------------------------------


def test_the_settings_of_older_tvs_need_the_password_again(app, authed_client, conn) -> None:
    client = authed_client(app, conn)
    for body in (
        {"older_tvs_enabled": True},
        {"older_tvs_http": True},
        {"tv_address": "http://192.168.1.10:8282"},
    ):
        assert client.put("/api/settings", json=body).status_code == 403, body


def test_both_switches_are_off_until_saved_on(app_factory, authed_client, conn) -> None:
    from fastapi.templating import Jinja2Templates

    from mediaman.app_factory import _TEMPLATE_DIR

    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    app = app_factory(settings_router, conn=conn, state_extras={"templates": templates})
    client = authed_client(app, conn, with_reauth=True)
    off = client.get("/settings").text
    sent = {"older_tvs_enabled": True, "older_tvs_http": True}
    assert client.put("/api/settings", json=sent).json()["ignored"] == []
    on = client.get("/settings").text
    for key in ("older_tvs_enabled", "older_tvs_http"):
        assert f'id="{key}" value="false"' in off
        assert f'id="{key}" value="true"' in on


@pytest.mark.parametrize(
    ("typed", "kept"),
    [
        ("http://192.168.1.10:8282/", "http://192.168.1.10:8282"),
        ("https://mediaman.example", "https://mediaman.example"),
        ("", ""),
    ],
)
def test_an_address_tvs_can_use_is_kept(client, typed, kept) -> None:
    assert client.put("/api/settings", json={"tv_address": typed}).status_code == 200
    assert client.get("/api/settings").json().get("tv_address", "") == kept


@pytest.mark.parametrize(
    "typed",
    [
        "http://localhost:8282",
        "https://localhost.",
        "http://127.0.0.1:8282",
        "http://[::1]:8282",
        "http://8.8.8.8",
        "http://mediaman.example",
        "ftp://192.168.1.10",
        "http://a:b@192.168.1.10",
        "http://192.168.1.10/path",
        "http://192.168.1.10:99999",
    ],
)
def test_an_address_tvs_cannot_use_is_refused(client, typed) -> None:
    assert client.put("/api/settings", json={"tv_address": typed}).status_code == 422
