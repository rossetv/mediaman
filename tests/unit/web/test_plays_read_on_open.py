"""Opening History or Profile reads the person's new Plex plays, at most once in five minutes."""

from __future__ import annotations

import pytest

from mediaman.services.plex_link import repository, sync
from mediaman.services.plex_link.plex_tv import PlexIdentity
from mediaman.web.routes.account import router as account_router
from mediaman.web.routes.history import router as history_router
from tests.helpers.fake_plex import SERVER_ID


class _Clock:
    now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(sync, "time", clock)
    monkeypatch.setattr(sync, "_opened", {})
    return clock


@pytest.fixture
def reads(monkeypatch):
    started: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        sync, "read_in_background", lambda db, key, name: started.append((db, key, name))
    )
    return started


@pytest.fixture
def client(app_factory, authed_client, conn, templates_stub, clock, reads):
    app = app_factory(
        history_router,
        account_router,
        conn=conn,
        state_extras={"templates": templates_stub, "db_path": "the.db"},
    )
    return authed_client(app, conn)


def _link(conn, name: str = "admin") -> None:
    repository.save_link(conn, name, PlexIdentity(4242, 4242, "amyplex"), SERVER_ID, ip="")


@pytest.mark.parametrize("page", ["/history?pane=1", "/account?pane=1"])
def test_opening_the_pane_reads_the_linked_persons_plays(client, conn, reads, secret_key, page):
    _link(conn)

    assert client.get(page).status_code == 200

    assert reads == [("the.db", secret_key, "admin")]


def test_a_person_with_no_linked_account_starts_no_read(client, reads):
    client.get("/history?pane=1")

    assert reads == []


def test_reads_come_once_in_five_minutes_for_each_person(client, conn, clock, reads):
    _link(conn)
    client.get("/history?pane=1")
    clock.now += 299
    client.get("/account?pane=1")
    clock.now += 1
    client.get("/history?pane=1")

    assert [name for _db, _key, name in reads] == ["admin", "admin"]
