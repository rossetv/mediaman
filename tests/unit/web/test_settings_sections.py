"""The server side of the settings page: what it offers, checks and refuses."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from fastapi.responses import JSONResponse
from jinja2 import Environment, FileSystemLoader

from mediaman.app_factory import _TEMPLATE_DIR
from mediaman.web.routes.settings import _SERVICE_TESTERS, _TEST_CACHE, router
from tests.helpers.factories import insert_settings


@pytest.fixture(autouse=True)
def _no_cached_tests():
    _TEST_CACHE.clear()
    yield
    _TEST_CACHE.clear()


@pytest.fixture
def client(app_factory, authed_client, conn, templates_stub):
    app = app_factory(router, conn=conn, state_extras={"templates": templates_stub})
    return authed_client(app, conn, with_reauth=True)


class _Selects(HTMLParser):
    """Each select's options, by its id: value, label and whether it is the chosen one."""

    def __init__(self) -> None:
        super().__init__()
        self.selects: dict[str, list[tuple[str, str, bool]]] = {}
        self._select = ""
        self._option: tuple[str, bool] | None = None
        self._text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        given = dict(attrs)
        if tag == "select":
            self._select = given.get("id") or ""
            self.selects[self._select] = []
        elif tag == "option":
            self._option, self._text = (given.get("value") or "", "selected" in given), ""

    def handle_data(self, data: str) -> None:
        self._text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._option:
            value, chosen = self._option
            label = re.sub(r"\s+", " ", self._text).strip()
            self.selects[self._select].append((value, label, chosen))
            self._option = None


def _general(client) -> dict[str, list[tuple[str, str, bool]]]:
    """The choices of the general section, drawn from what the page is given."""
    context = client.get("/settings").json()
    env = Environment(loader=FileSystemLoader(_TEMPLATE_DIR), autoescape=True)
    parser = _Selects()
    parser.feed(env.get_template("settings/_sec_general.html").render(**context))
    return parser.selects


def _chosen(options: list[tuple[str, str, bool]]) -> list[tuple[str, str]]:
    return [(value, label) for value, label, chosen in options if chosen]


def test_a_stored_value_outside_the_list_is_the_one_that_shows(client, conn) -> None:
    insert_settings(
        conn, scan_day="mon,thu", scan_timezone="US/Eastern", library_sync_interval="45"
    )
    selects = _general(client)
    assert _chosen(selects["scan_day"]) == [("mon,thu", "Mon, Thu")]
    assert _chosen(selects["scan_timezone"]) == [("US/Eastern", "US/Eastern")]
    assert _chosen(selects["library_sync_interval"]) == [("45", "Every 45 minutes")]
    # every other choice is still there
    assert {"mon", "sun"} <= {value for value, _, _ in selects["scan_day"]}


def test_a_stored_value_in_the_list_is_offered_once(client, conn) -> None:
    insert_settings(conn, scan_day="Wed", scan_timezone="Asia/Kolkata", library_sync_interval="60")
    selects = _general(client)
    assert _chosen(selects["scan_day"]) == [("wed", "Wednesday")]
    assert _chosen(selects["scan_timezone"]) == [("Asia/Kolkata", "Asia/Kolkata")]
    assert _chosen(selects["library_sync_interval"]) == [("60", "Every hour")]
    assert len(selects["scan_day"]) == 7
    for options in selects.values():
        values = [value for value, _, _ in options]
        assert len(values) == len(set(values))


def test_a_day_or_zone_nothing_can_read_shows_as_it_is_stored(client, conn) -> None:
    insert_settings(conn, scan_day="moon", scan_timezone="Nowhere/<b>")
    selects = _general(client)
    assert _chosen(selects["scan_day"]) == [("moon", "moon")]
    assert _chosen(selects["scan_timezone"]) == [("Nowhere/<b>", "Nowhere/<b>")]


def test_every_region_of_the_world_has_its_time_zones(client) -> None:
    zones = {value for value, _, _ in _general(client)["scan_timezone"]}
    assert {"UTC", "Asia/Kolkata", "America/Sao_Paulo", "Africa/Lagos", "Pacific/Auckland"} <= zones
    assert len(zones) > 300


def test_automatic_checks_leave_the_test_button_its_own_budget(client) -> None:
    for _ in range(20):
        assert client.post("/api/settings/test/openai?auto=1").status_code == 200
    assert client.post("/api/settings/test/openai").status_code == 200


def test_the_test_button_keeps_its_limit_beside_automatic_checks(client) -> None:
    for _ in range(10):
        assert client.post("/api/settings/test/openai").status_code == 200
    assert client.post("/api/settings/test/openai").status_code == 429
    assert client.post("/api/settings/test/openai?auto=1").status_code == 200


def test_automatic_checks_are_limited_too(client) -> None:
    codes = [client.post("/api/settings/test/openai?auto=1").status_code for _ in range(50)]
    assert codes[-1] == 429


def test_a_folder_the_page_may_not_read_is_not_called_missing(
    client, tmp_path, monkeypatch
) -> None:
    root = tmp_path.resolve() / "root"
    real = root / "real"
    real.mkdir(parents=True)
    (root / "link").symlink_to(real)
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))

    def answer(path) -> tuple[int, str]:
        resp = client.get("/api/settings/disk-usage", params={"path": str(path)})
        return resp.status_code, resp.json().get("error", "")

    assert answer(real) == (200, "")
    assert answer(root / "gone") == (404, "not_found")
    hidden = (403, "not_allowed")
    assert answer(root / "link") == hidden
    # nothing outside is told from what is not there at all
    assert answer(outside) == answer(outside / "gone") == hidden


def test_a_disk_that_cannot_be_read_says_so_in_a_sentence(client, monkeypatch) -> None:
    def broken(_path):
        raise PermissionError

    monkeypatch.setattr("mediaman.web.routes.settings.api.shutil.disk_usage", broken)
    body = client.get("/api/settings/disk-usage?path=/media/movies").json()
    assert body["error"] == "fetch_failed"
    assert body["message"].endswith(".")


def test_a_test_that_outlives_a_save_is_not_kept_for_the_new_settings(client, monkeypatch) -> None:
    def saved_meanwhile(_settings):
        body = {"openai_api_key": "a-new-key"}
        assert client.put("/api/settings", json=body).status_code == 200
        return JSONResponse({"ok": True})

    monkeypatch.setitem(_SERVICE_TESTERS, "openai", saved_meanwhile)
    assert client.post("/api/settings/test/openai").json()["ok"] is True
    monkeypatch.setitem(
        _SERVICE_TESTERS, "openai", lambda _settings: JSONResponse({"ok": False, "error": "x"})
    )
    assert client.post("/api/settings/test/openai").json()["ok"] is False


def test_a_test_that_a_save_did_not_touch_is_kept(client, monkeypatch) -> None:
    runs = []

    def tester(_settings):
        runs.append(1)
        assert client.put("/api/settings", json={"scan_time": "10:00"}).status_code == 200
        return JSONResponse({"ok": True})

    monkeypatch.setitem(_SERVICE_TESTERS, "openai", tester)
    for _ in range(2):
        assert client.post("/api/settings/test/openai").json()["ok"] is True
    assert len(runs) == 1


def test_a_secret_of_spaces_alone_never_replaces_the_stored_one(client, conn) -> None:
    assert client.put("/api/settings", json={"openai_api_key": "a-key"}).status_code == 200
    stored = conn.execute("SELECT value FROM settings WHERE key='openai_api_key'").fetchone()[0]
    assert client.put("/api/settings", json={"openai_api_key": "   "}).status_code == 422
    now = conn.execute("SELECT value FROM settings WHERE key='openai_api_key'").fetchone()[0]
    assert now == stored


@pytest.mark.parametrize(
    ("key", "label"),
    [
        ("base_url", "Base URL"),
        ("plex_url", "Plex URL"),
        ("sonarr_public_url", "Sonarr Public URL"),
    ],
)
def test_a_refused_address_is_named_as_the_page_names_it(client, monkeypatch, key, label) -> None:
    import socket

    def unknown(*_args, **_kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", unknown)
    body = client.put("/api/settings", json={key: "http://no.such.host.invalid/"}).json()
    assert body["error"] == "ssrf_blocked"
    assert body["message"].startswith(label + " ")
    assert "can't be found" in body["message"]
    assert key not in body["message"]


def test_a_blocked_address_is_still_refused_by_the_page_name(client) -> None:
    resp = client.put("/api/settings", json={"radarr_url": "http://169.254.169.254/"})
    assert resp.status_code == 400
    assert resp.json()["message"].startswith("Radarr URL ")


def test_an_address_with_a_password_in_it_is_refused_for_that(client) -> None:
    resp = client.put("/api/settings", json={"base_url": "http://me:secret@192.168.1.5/"})
    assert resp.status_code == 400
    message = resp.json()["message"]
    assert "password" in message and "secret" not in message
