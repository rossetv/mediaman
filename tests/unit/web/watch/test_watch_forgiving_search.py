"""Watch search forgives what people type: accents, a plural, a small typo, punctuation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.watch import browse_api
from tests.helpers.factories import insert_admin_user, insert_media_item, insert_settings


@pytest.fixture
def client(app_factory, conn):
    insert_settings(conn, plex_url="http://plex.invalid:32400", plex_token="tok")
    insert_settings(conn, plex_libraries='["1"]')
    insert_admin_user(conn, username="aoife", role="viewer")
    test_client = TestClient(app_factory(browse_api.router, conn=conn))
    test_client.cookies.set("session_token", create_session(conn, "aoife"))
    return test_client


def _show(conn, key: str, title: str) -> None:
    insert_media_item(
        conn,
        id=key,
        plex_rating_key=key,
        title=title,
        show_title=title,
        media_type="tv_season",
        season_number=1,
        show_rating_key=f"s{key}",
        plex_library_id=1,
    )


def _titles(client, q: str) -> list[str]:
    resp = client.get("/api/watch/search", params={"q": q})
    assert resp.status_code == 200
    return [i["title"] for i in resp.json()["items"]]


@pytest.mark.parametrize(
    ("typed", "title"),
    [
        ("90 days fiance", "90 Day Fiancé"),
        ("breking bad", "Breaking Bad"),
        ("the offices", "The Office"),
        ("greys anatomy", "Grey's Anatomy"),
        ("mission impossibel", "Mission: Impossible"),
    ],
)
def test_a_title_is_found_through_a_plural_a_typo_or_an_accent(client, conn, typed, title):
    _show(conn, "1", title)
    _show(conn, "2", "Something Else")

    assert _titles(client, typed) == [title]


def test_the_spin_offs_are_found_too(client, conn):
    _show(conn, "1", "90 Day Fiancé")
    _show(conn, "2", "90 Day Fiancé: Before the 90 Days")
    _show(conn, "3", "Love Island")

    assert _titles(client, "90 days fiance") == [
        "90 Day Fiancé",
        "90 Day Fiancé: Before the 90 Days",
    ]


def test_a_title_holding_what_was_typed_ranks_before_a_forgiven_one(client, conn):
    _show(conn, "1", "Bad Hair Days")
    _show(conn, "2", "Bad Hair Day")

    assert _titles(client, "bad hair days") == ["Bad Hair Days", "Bad Hair Day"]


@pytest.mark.parametrize("typed", ["dune", "cars"])
def test_a_short_word_is_not_stretched_to_another(client, conn, typed):
    _show(conn, "1", "June")
    _show(conn, "2", "Bars")

    assert _titles(client, typed) == []
