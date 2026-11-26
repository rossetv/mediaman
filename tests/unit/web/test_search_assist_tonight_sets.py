"""Tonight's sets: as long as the server makes them, where the next begins, whether the count is
all that fits, and no title from outside the library that is already on its way to it."""

# ruff: noqa: F811

from __future__ import annotations

from mediaman.services import watch_catalog as catalog
from mediaman.web.routes.search import assist
from tests.helpers.factories import insert_media_item
from tests.unit.web.test_search_assist import (  # noqa: F401  (fixtures)
    BASE,
    _world,
    amy,
    model,
    people,
    tmdb,
)


def _films(conn, tmdb, n):
    """*n* films of the library, each of 90 minutes, that TMDB has not been asked of yet."""
    for i in range(n):
        tmdb.films[f"Film {i}"] = (500 + i, 2000 + i, 90, "Someone")
        insert_media_item(
            conn, id=str(600 + i), title=f"Film {i}", media_type="movie", plex_library_id=1
        )
        conn.execute("UPDATE media_items SET tmdb_id = ? WHERE id = ?", (500 + i, str(600 + i)))
    conn.commit()


def _get(client, **params):
    resp = client.get(f"{BASE}/tonight", params={"minutes": 120, **params})
    assert resp.status_code == 200
    return resp.json()


def _known(client):
    """Asks until TMDB has been asked of every title, as the panel opened again does."""
    for _ in range(5):
        found = _get(client)
        if found["checked"]:
            return found
    raise AssertionError("never checked")


def test_seven_that_fit_come_as_a_set_of_six_then_one_where_the_server_says(amy, conn, tmdb):
    _films(conn, tmdb, 7)
    first = _known(amy)
    assert (len(first["items"]), first["total"], first["more"], first["next"]) == (6, 7, True, 6)

    second = _get(amy, skip=first["next"])
    assert (len(second["items"]), second["more"]) == (1, False)
    titles = {i["title"] for i in first["items"] + second["items"]}
    assert titles == {f"Film {i}" for i in range(7)}


def test_a_count_with_running_times_not_yet_looked_up_is_not_said_to_be_all(amy, conn, tmdb):
    _films(conn, tmdb, 7)
    first = _get(amy)
    assert first["checked"] is False and first["total"] < 7
    assert _known(amy)["total"] == 7


def test_a_title_downloaded_and_on_its_way_to_the_library_is_not_offered_from_elsewhere(
    amy, tmdb, monkeypatch
):
    tmdb.lists = {"popular_movies": [{"id": 2, "title": "Alibi"}, {"id": 3, "title": "Train Job"}]}
    card = catalog.tmdb_card

    def downloaded(item, *a, **k):
        found = card(item, *a, **k)
        return found and {**found, "download_state": "in_library" if item["id"] == 3 else None}

    monkeypatch.setattr(assist.catalog, "tmdb_card", downloaded)

    resp = amy.get(f"{BASE}/tonight/elsewhere", params={"minutes": 120})
    assert [i["title"] for i in resp.json()["items"]] == ["Alibi"]
