"""Tests for older TVs added by address: what an admin may ask, and what is kept of the answer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.db import get_db
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session
from mediaman.web.repository import older_tvs as repo
from mediaman.web.repository import tv as paired
from mediaman.web.routes import older_reach, older_stream, older_tvs
from mediaman.web.routes.settings import router as settings_router
from tests.helpers.factories import insert_settings
from tests.unit.services.dlna.fake_tv import (
    DESCRIPTION,
    TV,
    FakeTv,
    MovedTv,
    fault,
    http,
)

URL = "/api/settings/older-tvs"


@pytest.fixture
def tv(monkeypatch) -> FakeTv:
    fake = FakeTv()
    monkeypatch.setattr(older_reach, "Wire", fake.wire)
    return fake


@pytest.fixture
def app(app_factory, conn, tv):
    return app_factory(older_tvs.router, older_stream.router, settings_router, conn=conn)


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


MOVED = "192.168.1.77"


def test_a_kept_tv_that_moved_is_found_again_and_its_new_address_kept(
    client, conn, monkeypatch
) -> None:
    tv_id = _add(client).json()["tv"]["id"]
    monkeypatch.setattr(older_reach, "Wire", MovedTv(MOVED).wire)
    got = client.post(f"{URL}/{tv_id}/check")
    assert (got.status_code, got.json()["tv"]["address"]) == (200, MOVED)
    assert repo.every(conn)[0].address == MOVED
    row = conn.execute("SELECT * FROM audit_log WHERE action = 'sec:tv.moved'").fetchone()
    assert row["actor"] == "admin"
    assert TV in row["detail"]
    assert MOVED in row["detail"]


def test_a_moved_tv_is_not_looked_for_at_mediamans_own_address(client, conn, monkeypatch) -> None:
    tv_id = _add(client).json()["tv"]["id"]
    insert_settings(conn, tv_address=f"http://{MOVED}:8282")
    moved = MovedTv(MOVED)
    monkeypatch.setattr(older_reach, "Wire", moved.wire)
    assert client.post(f"{URL}/{tv_id}/check").status_code == 502
    assert not any(address == MOVED for _, (address, _) in moved.sent_udp)


def test_a_moved_tv_does_not_take_another_kept_tvs_address(client, conn, monkeypatch) -> None:
    tv_id = _add(client).json()["tv"]["id"]
    conn.execute(
        "INSERT INTO tvs (kind, name, address, udn, created_at)"
        f" VALUES ('older', 'Other', '{MOVED}', 'uuid:9', '2026-01-01')"
    )
    conn.commit()
    monkeypatch.setattr(older_reach, "Wire", MovedTv(MOVED).wire)
    assert client.post(f"{URL}/{tv_id}/check").status_code == 502
    assert repo.find(conn, tv_id).address == TV


def test_a_tv_that_tells_no_id_is_not_looked_for(client, tv, conn, monkeypatch) -> None:
    tv.pages["/smp_14_"] = http(DESCRIPTION.replace("uuid:1234-abcd", ""))
    tv_id = _add(client).json()["tv"]["id"]
    moved = MovedTv(MOVED)
    monkeypatch.setattr(older_reach, "Wire", moved.wire)
    assert client.post(f"{URL}/{tv_id}/check").json()["error"] == "unreachable"
    assert {address for _, (address, _) in moved.sent_udp} == {TV}


@pytest.mark.parametrize("key", ["tv_address", "base_url"])
def test_mediamans_own_address_is_not_asked_under_a_name_either(
    client, tv, conn, key, monkeypatch
) -> None:
    insert_settings(conn, **{key: "https://media.example"})
    monkeypatch.setattr(older_reach.socket, "getaddrinfo", lambda *a: [(2, 1, 6, "", (TV, 0))])
    got = client.post(f"{URL}/check", json={"address": TV})
    assert (got.status_code, got.json()["error"]) == (422, "own_address")
    assert not _asked(tv)


# ── the test picture ──


HOST = "http://192.168.1.10:8282"


@pytest.fixture
def shown(app, client, conn, tv, monkeypatch):
    """A TV that fetches what it is handed, and how often it was told to stop by then."""
    insert_settings(conn, tv_address=HOST, older_tvs_http="true")
    monkeypatch.setattr(older_tvs, "_FETCH_SECONDS", 0.05)
    monkeypatch.setattr(older_tvs, "_SHOWN_SECONDS", 0.0)
    real, page = tv.reply, TestClient(app)
    tv.urls, tv.fetches, tv.seen = [], True, []

    def reply(request):
        if b"#SetAVTransportURI" in request:
            url = request.partition(b"<CurrentURI>")[2].partition(b"</CurrentURI>")[0].decode()
            tv.urls.append(url)
            if tv.fetches:
                tv.seen.append(page.get(url.removeprefix(HOST)).content[:3])
        return real(request)

    tv.reply = reply
    return tv


def _picture(client):
    return client.post(f"{URL}/picture", json={"address": TV})


def _stops(tv) -> int:
    return sum(b"#Stop" in request for request in tv.requests)


def test_the_test_picture_is_fetched_from_the_address_set_and_then_put_away(shown, client) -> None:
    got = _picture(client)
    assert got.json() == {"ok": True, "from": HOST}
    (url,) = shown.urls
    assert url.startswith(f"{HOST}/api/cast/")
    assert shown.seen == [b"\xff\xd8\xff"]
    assert _stops(shown) == 1
    assert client.get(url.removeprefix(HOST)).status_code == 404


def test_a_tv_that_does_not_come_for_the_picture_cannot_reach_mediaman(shown, client) -> None:
    shown.fetches = False
    got = _picture(client)
    assert (got.status_code, got.json()["error"]) == (502, "no_fetch")
    assert _stops(shown) == 1


def test_a_tv_that_shows_no_pictures_is_told_from_one_that_cannot_reach_mediaman(
    shown, client
) -> None:
    shown.fetches = False
    shown.actions["SetAVTransportURI"] = fault("714")
    got = _picture(client)
    assert (got.status_code, got.json()["error"]) == (502, "no_picture")


def test_a_tv_that_fetches_the_picture_has_reached_mediaman_whatever_it_says(shown, client) -> None:
    shown.actions["SetAVTransportURI"] = fault("714")
    assert _picture(client).status_code == 200


@pytest.mark.parametrize(
    ("settings", "error"),
    [({"tv_address": ""}, "no_tv_address"), ({"older_tvs_http": "false"}, "http_off")],
)
def test_no_picture_is_shown_without_an_address_tvs_may_use(
    shown, client, conn, settings, error
) -> None:
    for key, value in settings.items():
        conn.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
    conn.commit()
    got = _picture(client)
    assert (got.status_code, got.json()["error"]) == (409, error)
    assert shown.urls == []


@pytest.mark.parametrize("kept", ["https://a b", "https://*.example", "ftp://x", "x"])
def test_an_address_kept_before_that_no_tv_can_use_is_told(shown, client, conn, kept) -> None:
    conn.execute("UPDATE settings SET value = ? WHERE key = 'tv_address'", (kept,))
    conn.commit()
    got = _picture(client)
    assert (got.status_code, got.json()["error"]) == (409, "bad_tv_address")
    assert "Settings" in got.json()["message"]
    assert shown.urls == []


def test_a_host_the_app_does_not_answer_to_is_named_with_what_to_do(
    shown, client, monkeypatch
) -> None:
    monkeypatch.setenv("MEDIAMAN_ALLOWED_HOSTS", "mediaman.example, *.other.example")
    got = _picture(client)
    assert (got.status_code, got.json()["error"]) == (409, "host_not_allowed")
    assert got.json()["message"] == (
        "MEDIAMAN_ALLOWED_HOSTS does not hold 192.168.1.10. Add it and restart mediaman."
    )
    assert shown.urls == []


@pytest.mark.parametrize(
    ("allowed", "address"),
    [
        ("mediaman.example,192.168.1.10", HOST),
        ("*", HOST),
        ("", HOST),
        ("*.other.example", "https://TV.other.example"),
    ],
)
def test_a_host_the_app_answers_to_is_used(
    shown, client, conn, monkeypatch, allowed, address
) -> None:
    monkeypatch.setattr(older_reach.socket, "getaddrinfo", lambda *a: [])
    monkeypatch.setenv("MEDIAMAN_ALLOWED_HOSTS", allowed)
    conn.execute("UPDATE settings SET value = ? WHERE key = 'tv_address'", (address,))
    conn.commit()
    _picture(client)
    assert len(shown.urls) == 1


def test_https_needs_no_switch(shown, client, conn, monkeypatch) -> None:
    monkeypatch.setattr(older_reach.socket, "getaddrinfo", lambda *a: [])
    conn.execute("UPDATE settings SET value = 'false' WHERE key = 'older_tvs_http'")
    conn.execute("UPDATE settings SET value = 'https://tv.example' WHERE key = 'tv_address'")
    conn.commit()
    _picture(client)
    assert shown.urls[0].startswith("https://tv.example/api/cast/")


def test_the_address_is_never_taken_from_the_request(shown, client) -> None:
    client.post(
        f"{URL}/picture",
        json={"address": TV},
        headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"},
    )
    assert all(url.startswith(HOST) for url in shown.urls) and shown.urls


def test_a_kept_tv_which_may_be_playing_is_shown_no_picture(shown, client) -> None:
    assert _add(client).status_code == 200
    assert _picture(client).json()["error"] == "already_added"
    assert shown.urls == []


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
    with older_reach.looking(), older_reach.looking():
        got = client.post(f"{URL}/check", json={"address": TV})
        assert (got.status_code, got.json()["error"]) == (503, "busy")
        assert not _asked(tv)
    assert client.post(f"{URL}/check", json={"address": TV}).status_code == 200


def test_a_person_looks_for_one_tv_at_a_time(client, tv) -> None:
    with older_reach.looking("person admin"):
        got = client.post(f"{URL}/check", json={"address": TV})
        assert (got.status_code, got.json()["error"]) == (409, "looking")
        assert not _asked(tv)
    assert client.post(f"{URL}/check", json={"address": TV}).status_code == 200


def test_a_tv_is_looked_for_by_one_person_at_a_time(client, tv) -> None:
    with older_reach.looking("person amy", f"tv {TV}"):
        got = client.post(f"{URL}/check", json={"address": TV})
        assert (got.status_code, got.json()["error"]) == (409, "looking")
        assert not _asked(tv)


def test_a_test_picture_on_show_keeps_nobody_from_looking(shown, client, monkeypatch) -> None:
    looked = []
    real = older_tvs._show_picture

    def show(*args):
        with older_reach.looking("person amy"), older_reach.looking("person bob"):
            looked.append(True)
        return real(*args)

    monkeypatch.setattr(older_tvs, "_show_picture", show)
    assert _picture(client).status_code == 200
    assert looked == [True]


def test_one_test_picture_is_shown_at_a_time(shown, client) -> None:
    with older_tvs._SHOWS:
        got = _picture(client)
    assert (got.status_code, got.json()["error"]) == (503, "busy")
    assert shown.urls == []


def test_checks_are_limited(client, tv) -> None:
    answers = [
        client.post(f"{URL}/check", json={"address": "8.8.8.8"}).status_code for _ in range(21)
    ]
    assert answers == [422] * 20 + [429]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "", {"address": TV, "name": "x"}),
        ("POST", "/check", {"address": TV}),
        ("POST", "/picture", {"address": TV}),
        ("PATCH", "/1", {"name": "Den"}),
    ],
)
def test_a_session_that_ended_while_the_body_came_changes_nothing(
    app, client, conn, tv, method, path, body
) -> None:
    tv_id = _add(client).json()["tv"]["id"]
    app.dependency_overrides[get_current_admin] = lambda: "admin"
    conn.execute("DELETE FROM admin_sessions")
    conn.commit()
    tv.sent_udp.clear()
    got = client.request(method, f"{URL}{path.replace('1', str(tv_id))}", json=body)
    assert (got.status_code, got.json()) == (401, {"detail": "Not authenticated"})
    assert not tv.sent_udp
    assert [kept.name for kept in repo.every(conn)] == ["Kitchen TV"]


def test_an_admin_who_is_one_no_more_by_then_changes_nothing(app, client, conn) -> None:
    tv_id = _add(client).json()["tv"]["id"]
    app.dependency_overrides[get_current_admin] = lambda: "admin"
    conn.execute("UPDATE admin_users SET role = 'viewer'")
    conn.commit()
    assert client.patch(f"{URL}/{tv_id}", json={"name": "Den"}).status_code == 401
    assert repo.every(conn)[0].name == "Kitchen TV"


@pytest.mark.parametrize("ask", [_add, _picture])
def test_a_session_that_ended_while_the_tv_was_asked_adds_and_shows_nothing(
    shown, client, conn, monkeypatch, ask
) -> None:
    real = older_reach.find

    def slow(*args):
        found = real(*args)
        with get_db() as here:
            here.execute("DELETE FROM admin_sessions")
        return found

    monkeypatch.setattr(older_reach, "find", slow)
    assert ask(client).status_code == 401
    assert repo.every(conn) == []
    assert shown.urls == []


def test_a_viewer_reaches_none_of_it(app, client, conn) -> None:
    tv_id = _add(client).json()["tv"]["id"]
    create_user(conn, "amy", "a-long-enough-Passw0rd!", role="viewer", enforce_policy=False)
    viewer = TestClient(app)
    viewer.cookies.set("session_token", create_session(conn, "amy"))
    for method, url, body in (
        ("GET", URL, None),
        ("POST", URL, {"address": TV, "name": "x"}),
        ("POST", f"{URL}/check", {"address": TV}),
        ("POST", f"{URL}/picture", {"address": TV}),
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
        ("https://Media-Man.example:8443/", "https://media-man.example:8443"),
        ("https://XN--Fernseh-Gert-qcb.example", "https://xn--fernseh-gert-qcb.example"),
        ("https://4k.example", "https://4k.example"),
        ("https://192.168.1.10", "https://192.168.1.10"),
        ("http://100.64.1.2:8282", "http://100.64.1.2:8282"),
        ("http://[FD00::5]:8282/", "http://[fd00::5]:8282"),
        ("https://8.8.8.8", "https://8.8.8.8"),
        ("https://[2001:db8::1]:8443", "https://[2001:db8::1]:8443"),
        ("http://NAS.local:8282", "http://nas.local:8282"),
        ("http://mediaman.:8282", "http://mediaman:8282"),
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
        "http://100.128.0.1",
        "http://[2001:db8::1]",
        "http://[::ffff:8.8.8.8]",
        "http://[fe80::1]:8282",
        "https://[fe80::1]",
        "https://127.0.0.1",
        "https://[::ffff:127.0.0.1]",
        "https://0.0.0.0",
        "http://mediaman.example",
        "http://nas.local.example",
        "javascript://192.168.1.10",
        "ftp://192.168.1.10",
        "http://a:b@192.168.1.10",
        "http://192.168.1.10/path",
        "http://192.168.1.10:99999",
        "http://192.168.1.10:0",
        "https://a b",
        "https://a\tb.example",
        "https://a\x00b.example",
        "https://mediaman.example\x7f",
        " https://mediaman.example",
        "https://mediaman.example ",
        "https://mediaman.example\u00a0",
        "https://faß.example",
        "https://fernseh-gerät.example",
        "https://mediaman。example",
        "https://１９２.168.1.10",
        'https://a"b.example',
        "https://*.example",
        "https://a_b.example",
        "https://-a.example",
        "https://a..example",
        "https://12345",
        "https://1.2.3",
        "https://mediaman.0x7f",
        "https://[::1]",
        "https://" + "a" * 64 + ".example",
    ],
)
def test_an_address_tvs_cannot_use_is_refused(client, typed) -> None:
    assert client.put("/api/settings", json={"tv_address": typed}).status_code == 422
