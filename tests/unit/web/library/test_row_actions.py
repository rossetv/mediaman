"""The Library page's row buttons, rendered with the real template.

A grouped TV show row stands for several seasons, so its Delete must never
carry a deletable item id; it carries the show key the season list is
fetched with. Values encoded with tojson in data-* attributes must reach the
JS intact whatever the title holds.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.templating import Jinja2Templates

from mediaman.web.routes.kept import router as kept_router
from mediaman.web.routes.library import router as pages_router
from tests.helpers.factories import insert_kept_show, insert_media_item, insert_scheduled_action

_TEMPLATE_DIR = Path(__file__).parents[4] / "src" / "mediaman" / "web" / "templates"
_AWKWARD_TEXT = """He said "Hi" & it's <b>bold</b>"""
_SEASON_IDS = {"s10", "s2", "s3"}


class _Elements(HTMLParser):
    """Collect the attributes of every start tag."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[dict[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        self.elements.append(dict(attrs))


def _elements_with(html: str, attr: str) -> list[dict[str, str | None]]:
    parser = _Elements()
    parser.feed(html)
    return [e for e in parser.elements if attr in e]


def _delete_buttons(html: str) -> list[dict[str, str | None]]:
    return [
        e
        for e in _elements_with(html, "data-action")
        if e["data-action"] in ("confirm-delete", "pick-season-delete")
    ]


@pytest.fixture
def client(app_factory, authed_client, conn):
    app = app_factory(
        pages_router,
        kept_router,
        conn=conn,
        state_extras={"templates": Jinja2Templates(directory=str(_TEMPLATE_DIR))},
    )
    return authed_client(app, conn)


def _insert_show(conn, show_rating_key: str | None, show_title: str = "Breaking Bad") -> None:
    for media_id in _SEASON_IDS:
        insert_media_item(
            conn,
            id=media_id,
            title=f"{show_title} {media_id}",
            media_type="tv_season",
            show_title=show_title,
            season_number=int(media_id[1:]),
            plex_rating_key=f"rk-{media_id}",
            show_rating_key=show_rating_key,
            file_path=f"/media/tv/Breaking Bad/{media_id}",
            file_size_bytes=1_000,
        )


def _insert_movie(conn, media_id: str, title: str) -> None:
    insert_media_item(
        conn,
        id=media_id,
        title=title,
        plex_rating_key=f"rk-{media_id}",
        file_path="/f",
        file_size_bytes=1_000,
    )


@pytest.mark.parametrize("show_rating_key", ["show-rk", None])
def test_show_row_delete_carries_no_season_id(client, conn, show_rating_key):
    _insert_show(conn, show_rating_key)

    resp = client.get("/library")

    assert resp.status_code == 200
    (button,) = _delete_buttons(resp.text)
    assert "data-id" not in button
    assert button["data-show-rk"] == (show_rating_key or "Breaking Bad")


def test_show_row_delete_ignores_the_label_wording(client, conn, monkeypatch):
    from mediaman.web.repository.library_query import _query

    real = _query._shape_rows

    def reworded(*args, **kwargs):
        items = real(*args, **kwargs)
        for item in items:
            item["type_label"] = "1 season"
        return items

    monkeypatch.setattr(_query, "_shape_rows", reworded)
    _insert_show(conn, "show-rk")

    (button,) = _delete_buttons(client.get("/library").text)

    assert button["data-action"] == "pick-season-delete"
    assert "data-id" not in button


def test_single_season_show_row_deletes_directly(client, conn):
    insert_media_item(
        conn,
        id="solo",
        title="Solo S1",
        media_type="tv_season",
        show_title="Solo",
        season_number=1,
        plex_rating_key="rk-solo",
        show_rating_key="solo-rk",
        file_path="/media/tv/Solo",
        file_size_bytes=1_000,
    )

    (button,) = _delete_buttons(client.get("/library").text)

    assert button["data-action"] == "confirm-delete"
    assert button["data-id"] == "solo"


@pytest.mark.parametrize("show_rating_key", ["show-rk", None])
def test_season_list_from_show_row_holds_every_grouped_season(client, conn, show_rating_key):
    _insert_show(conn, show_rating_key)
    (button,) = _delete_buttons(client.get("/library").text)

    resp = client.get(f"/api/show/{button['data-show-rk']}/seasons")

    assert resp.status_code == 200
    body = resp.json()
    assert {s["id"] for s in body["seasons"]} == _SEASON_IDS
    assert [s["season_number"] for s in body["seasons"]] == [2, 3, 10]
    assert body["show_title"] == "Breaking Bad"


def test_season_list_opens_for_a_title_with_a_slash(client, conn):
    _insert_show(conn, None, show_title="Love/Hate")
    (button,) = _delete_buttons(client.get("/library").text)

    # Encoded as the page's script encodes it, with encodeURIComponent.
    resp = client.get(f"/api/show/{quote(button['data-show-rk'] or '', safe='')}/seasons")

    assert resp.status_code == 200
    assert {s["id"] for s in resp.json()["seasons"]} == _SEASON_IDS


def test_season_list_leaves_out_another_shows_seasons(client, conn):
    _insert_show(conn, None)
    insert_media_item(
        conn,
        id="other",
        title="Other S1",
        media_type="tv_season",
        show_title="Other",
        season_number=1,
        plex_rating_key="rk-other",
        show_rating_key="other-rk",
        file_path="/media/tv/Other",
        file_size_bytes=1_000,
    )

    resp = client.get("/api/show/Breaking Bad/seasons")

    assert {s["id"] for s in resp.json()["seasons"]} == _SEASON_IDS


def test_movie_delete_carries_its_id_and_a_quoted_title(client, conn):
    _insert_movie(conn, "m1", _AWKWARD_TEXT)

    (button,) = _delete_buttons(client.get("/library").text)

    assert button["data-action"] == "confirm-delete"
    assert button["data-id"] == "m1"
    assert json.loads(button["data-title"] or "") == _AWKWARD_TEXT


def test_remove_keep_carries_a_quoted_id(client, conn):
    _insert_movie(conn, _AWKWARD_TEXT, "Dune")
    insert_scheduled_action(
        conn,
        media_item_id=_AWKWARD_TEXT,
        action="protected_forever",
        execute_at=None,
        token="tok-kept",
    )

    (button,) = _elements_with(client.get("/library").text, "data-remove-keep-id")

    assert json.loads(button["data-remove-keep-id"] or "") == _AWKWARD_TEXT


def _keep_wrappers(html: str) -> list[dict[str, str | None]]:
    return [
        e for e in _elements_with(html, "data-show-rk") if "keep-wrapper" in (e.get("class") or "")
    ]


@pytest.mark.parametrize(("show_rating_key", "expected"), [("show-rk", "true"), (None, "false")])
def test_show_row_flags_whether_its_key_is_a_rating_key(client, conn, show_rating_key, expected):
    _insert_show(conn, show_rating_key)

    (wrapper,) = _keep_wrappers(client.get("/library").text)

    assert wrapper["data-has-rk"] == expected
    assert wrapper["data-show-rk"] == (show_rating_key or "Breaking Bad")


def _keep(conn, media_id: str) -> None:
    insert_scheduled_action(
        conn,
        media_item_id=media_id,
        action="protected_forever",
        execute_at=None,
        token=f"tok-{media_id}",
    )


def test_partly_kept_show_row_keeps_its_delete_and_says_how_many_are_kept(client, conn):
    _insert_show(conn, "show-rk")
    _keep(conn, "s3")

    html = client.get("/library").text

    assert "lib-row-protected" not in html
    assert "1 of 3 seasons kept" in html
    assert [b["data-action"] for b in _delete_buttons(html)] == ["pick-season-delete"]
    assert not _elements_with(html, "data-remove-keep-id")


def test_remove_keep_on_a_fully_kept_show_row_opens_the_keep_dialog(client, conn):
    _insert_show(conn, "show-rk")
    for media_id in _SEASON_IDS:
        _keep(conn, media_id)

    html = client.get("/library").text

    assert not _elements_with(html, "data-remove-keep-id")
    (button,) = _elements_with(html, "data-remove-keep-dialog")
    assert "snooze-option--remove" in (button.get("class") or "")


def test_remove_keep_on_a_single_season_show_kept_as_a_whole_opens_the_keep_dialog(client, conn):
    insert_media_item(
        conn,
        id="s1",
        title="Show S01",
        media_type="tv_season",
        show_title="Show",
        season_number=1,
        plex_rating_key="rk-s1",
        show_rating_key="show-rk",
        file_path="/f",
        file_size_bytes=1_000,
    )
    insert_kept_show(conn, show_rating_key="show-rk", action="protected_forever")

    html = client.get("/library").text

    assert not _elements_with(html, "data-remove-keep-id")
    assert len(_elements_with(html, "data-remove-keep-dialog")) == 1
