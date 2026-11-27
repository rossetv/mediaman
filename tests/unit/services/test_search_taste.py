"""The search sheet's categories and suggestions for one person, from their own plays and
ratings over the library's held genres and cast."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from mediaman.services import search_taste as taste_mod
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.openai import client as openai_client
from mediaman.services.search_taste import DEFAULT_ORDER, search_taste
from mediaman.services.watch_catalog import GENRES, _title_meta
from mediaman.web.repository.watch_progress import save_progress
from tests.helpers.factories import insert_admin_user, insert_media_item, insert_settings

NOW = datetime(2026, 6, 15, 12, tzinfo=UTC)


@pytest.fixture
def lib(conn, monkeypatch):
    monkeypatch.setattr(_title_meta, "_library", {})
    insert_settings(conn, plex_libraries='["1"]')

    def title(tmdb_id, genres, *, cast=(), name=None):
        insert_media_item(
            conn, id=str(tmdb_id), plex_rating_key=str(tmdb_id), title=name or f"Film {tmdb_id}",
            media_type="movie", plex_library_id=1,
        )  # fmt: skip
        conn.execute("UPDATE media_items SET tmdb_id = ? WHERE id = ?", (tmdb_id, str(tmdb_id)))
        details = {
            "title": name or f"Film {tmdb_id}", "released": "2020-01-01", "poster": None,
            "rating": 7.0, "votes": 100, "popularity": 1.0, "genres": list(genres),
        }  # fmt: skip
        conn.execute(
            "INSERT INTO title_meta (id_type, id, tmdb_id, details, cast_json, fetched_at)"
            " VALUES ('movie', ?, ?, ?, ?, ?)",
            (
                tmdb_id,
                tmdb_id,
                json.dumps(details),
                json.dumps([list(c) for c in cast]),
                "2026-06-01",
            ),
        )
        conn.commit()

    return title


def _person(conn, username):
    user_id = insert_admin_user(conn, username=username)

    def played(tmdb_id, days_ago, name=None):
        at = (NOW - timedelta(days=days_ago)).isoformat()
        conn.execute(
            "INSERT INTO played_titles (username, media_type, title, tmdb_id, played_at)"
            " VALUES (?, 'movie', ?, ?, ?)",
            (username, name or f"Film {tmdb_id}", tmdb_id, at),
        )
        conn.commit()

    def rated(tmdb_id, level, name=None):
        conn.execute(
            "INSERT INTO user_ratings (user_id, media_type, tmdb_id, level, title, updated_at)"
            " VALUES (?, 'movie', ?, ?, ?, ?)",
            (user_id, tmdb_id, level, name, NOW.isoformat()),
        )
        conn.commit()

    return user_id, played, rated


def _taste(conn, username, user_id):
    return search_taste(conn, username, user_id, NOW)


def test_recent_plays_lead_rated_down_goes_last_and_every_category_stays(conn, lib):
    user_id, played, rated = _person(conn, "ann")
    for tmdb_id, genre in (
        (1, "comedy"),
        (2, "comedy"),
        (3, "romance"),
        (4, "action"),
        (5, "horror"),
    ):
        lib(tmdb_id, [genre])
    played(1, 0)
    played(2, 3)
    played(3, 0)  # today: counts 1
    played(4, 60)  # two months ago: counts half, though action stands first by default
    played(5, 0)
    rated(5, -2)

    order = _taste(conn, "ann", user_id)["order"]

    assert order[:3] == ["comedy", "romance", "action"]
    assert order[-1] == "horror"
    assert sorted(order) == sorted(GENRES)
    assert order[3:-1] == [
        g for g in DEFAULT_ORDER if g not in ("comedy", "romance", "action", "horror")
    ]


def test_a_title_of_many_genres_counts_for_each_by_the_square_root_of_their_number(conn, lib):
    user_id, played, rated = _person(conn, "ann")
    lib(1, ["action", "comedy", "drama", "thriller"])
    lib(2, ["romance"])
    lib(3, ["crime", "mystery"])
    lib(4, ["crime", "mystery"])
    lib(5, ["crime", "mystery"])
    for tmdb_id in (1, 2, 3, 4, 5):
        played(tmdb_id, 0)
    rated(2, 1)
    lib(6, ["family"])
    played(6, 30)  # 0.7

    order = _taste(conn, "ann", user_id)["order"]

    # Three two-genre titles: 3 / sqrt 2 = 2.1 each, above romance's 2 (a play and a +1);
    # one of four genres: 1 / sqrt 4 = 0.5, under family's single 0.7.
    assert order[:6] == ["crime", "mystery", "romance", "family", "action", "comedy"]


def test_favourites_need_two_titles_and_a_third_of_the_top_score_and_are_three_at_most(conn, lib):
    user_id, played, _rated = _person(conn, "ann")
    plan = {
        "comedy": [0, 0, 0, 0, 0, 0],  # 6
        "romance": [0, 0, 0],  # 3
        "crime": [0, 0],  # 2: a third of the top
        "drama": [60, 60],  # 1: under a third
        "horror": [0],  # 1 title alone
        "action": [0, 0],  # 2, but a fourth
    }
    tmdb_id = 0
    for genre, days in plan.items():
        for ago in days:
            tmdb_id += 1
            lib(tmdb_id, [genre])
            played(tmdb_id, ago)

    found = _taste(conn, "ann", user_id)

    assert found["favourites"] == ["comedy", "romance", "action"]
    assert "horror" not in found["favourites"] and "drama" not in found["favourites"]


def test_a_third_of_the_top_with_two_titles_is_a_favourite_and_one_title_alone_is_not(conn, lib):
    user_id, played, rated = _person(conn, "ann")
    for tmdb_id in range(1, 7):
        lib(tmdb_id, ["comedy"])
        played(tmdb_id, 0)
    for tmdb_id in (7, 8):
        lib(tmdb_id, ["crime"])
        played(tmdb_id, 0)
    lib(9, ["drama"])
    lib(10, ["drama"])
    played(9, 70)
    played(10, 70)
    lib(11, ["horror"])
    played(11, 0)
    rated(11, 2)  # 3: well above a third, but one title

    assert _taste(conn, "ann", user_id)["favourites"] == ["comedy", "crime"]


def test_someone_with_under_three_library_plays_gets_the_usual_sheet(conn, lib):
    user_id, played, rated = _person(conn, "new")
    lib(1, ["horror"])
    lib(2, ["horror"])
    lib(3, ["horror"])
    played(1, 0)
    played(2, 0)
    played(99, 0)  # not in the library
    rated(3, 2)  # rated, never played

    assert _taste(conn, "new", user_id) == {
        "personal": False,
        "order": list(DEFAULT_ORDER),
        "favourites": [],
        "chips": [],
        "like": None,
    }


def test_suggestions_come_from_the_top_two_categories_the_most_seen_actor_and_the_best_liked(
    conn, lib
):
    user_id, played, rated = _person(conn, "ann")
    star = (501, "Ines Varga", "/ines.jpg")
    lib(1, ["comedy"], cast=[star, (502, "Bo", None)])
    lib(2, ["comedy"], cast=[star])
    lib(3, ["comedy"], cast=[(503, "Cy", None)])
    lib(6, ["comedy"], cast=[star])
    lib(4, ["science-fiction"], cast=[(503, "Cy", None)])
    lib(5, ["science-fiction"], name="Rick and Morty")
    for tmdb_id in (1, 2, 3, 4, 6):
        played(tmdb_id, 0)
    played(5, 100, name="Rick and Morty")
    rated(5, 2, name="Rick and Morty")  # 0.3 for the play + 2: liked most

    found = _taste(conn, "ann", user_id)

    assert [(c["kind"], c.get("genre"), c.get("sort")) for c in found["chips"]] == [
        ("genre", "comedy", "popular"),
        ("genre", "science-fiction", "top_rated"),
        ("person", None, None),
        ("genre", "comedy", "newest"),
    ]
    person = found["chips"][2]
    assert (person["person_id"], person["name"]) == (501, "Ines Varga")
    assert person["profile_url"] and "ines.jpg" in person["profile_url"]
    assert all(c["type"] == "movie" for c in found["chips"] if c["kind"] == "genre")
    assert found["like"] == {
        "ref": "tmdb-movie-5",
        "name": "Rick and Morty",
        "text": "More like Rick and Morty",
        "poster_url": "/api/poster/5",
    }


def test_no_more_like_when_nothing_of_the_library_was_liked_lately_nor_an_actor_seen_twice(
    conn, lib
):
    user_id, played, rated = _person(conn, "ann")
    for tmdb_id in (1, 2, 3):
        lib(tmdb_id, ["drama"], cast=[(600 + tmdb_id, "Someone", None)])
        played(tmdb_id, 30)
    rated(99, 2, name="Not in the library")

    found = _taste(conn, "ann", user_id)

    assert found["personal"] is True
    assert found["like"] is None
    assert [c["kind"] for c in found["chips"]] == ["genre", "genre"]


def test_left_part_way_counts_less_and_watched_again_more(conn, lib, monkeypatch):
    user_id, _played, _rated = _person(conn, "ann")
    for tmdb_id, genre in ((1, "action"), (2, "comedy"), (3, "drama"), (4, "thriller")):
        lib(tmdb_id, [genre])
    notes = {1: "left part way", 2: "", 3: "watched again", 4: "left after 2 episodes"}

    def noted(_conn, _username):
        return [_Play(tmdb_id, note) for tmdb_id, note in notes.items()]

    monkeypatch.setattr(taste_mod, "person_plays", noted)

    order = _taste(conn, "ann", user_id)["order"]

    assert order[:4] == ["drama", "comedy", "action", "thriller"]


class _Play:
    kind = "movie"

    def __init__(self, tmdb_id, note):
        self.tmdb_id = tmdb_id
        self.note = note
        self.title = f"Film {tmdb_id}"
        self.last_played = NOW.isoformat()


def test_two_people_get_their_own_order_and_neither_sees_the_others_titles(conn, lib):
    ann, ann_played, ann_rated = _person(conn, "ann")
    bob, bob_played, _ = _person(conn, "bob")
    for tmdb_id in (1, 2, 3):
        lib(tmdb_id, ["comedy"], name=f"Ann film {tmdb_id}")
        ann_played(tmdb_id, 0, name=f"Ann film {tmdb_id}")
    ann_rated(1, 2, name="Ann film 1")
    for tmdb_id in (4, 5, 6):
        lib(tmdb_id, ["romance"])
        bob_played(tmdb_id, 0)

    found_ann = _taste(conn, "ann", ann)
    found_bob = _taste(conn, "bob", bob)

    assert found_ann["order"][0] == "comedy"
    assert found_bob["order"][0] == "romance"
    assert "comedy" not in found_bob["favourites"]
    assert "Ann" not in json.dumps(found_bob)


def test_it_asks_neither_tmdb_nor_the_ai(conn, lib, monkeypatch):
    def never(*_a, **_k):
        raise AssertionError("no outside call")

    monkeypatch.setattr(TmdbClient, "_get", never)
    monkeypatch.setattr(TmdbClient, "from_db", never)
    monkeypatch.setattr(openai_client, "ask_openai", never)
    monkeypatch.setattr(openai_client, "call_openai", never)
    user_id, played, rated = _person(conn, "ann")
    for tmdb_id in (1, 2, 3):
        lib(tmdb_id, ["comedy"], cast=[(700, "Al", None)])
        played(tmdb_id, 0)
    rated(1, 2)

    assert _taste(conn, "ann", user_id)["personal"] is True


def _counting(monkeypatch, module, name):
    calls = []
    real = getattr(module, name)

    def counted(*args, **kwargs):
        calls.append(name)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    return calls


def test_a_second_load_with_nothing_new_reads_neither_the_plays_nor_the_whole_library(
    conn, lib, monkeypatch
):
    user_id, played, rated = _person(conn, "ann")
    for tmdb_id in (1, 2, 3, 4):
        lib(tmdb_id, ["comedy"])
    for tmdb_id in (1, 2, 3):
        played(tmdb_id, 1)
    first = _taste(conn, "ann", user_id)
    reads = _counting(monkeypatch, taste_mod, "person_plays")
    reads += _counting(monkeypatch, _title_meta, "_all_there")
    reads += _counting(monkeypatch, _title_meta, "_build")
    # Long past the minute the library's lists are held for.
    monkeypatch.setattr(_title_meta, "_clock", lambda: 1e12)

    assert _taste(conn, "ann", user_id) == first
    assert reads == []


def test_a_saved_position_alone_does_not_read_the_plays_again_but_a_new_credit_does(
    conn, lib, monkeypatch
):
    user_id, played, _ = _person(conn, "ann")
    for tmdb_id in (1, 2, 3, 4, 5):
        lib(tmdb_id, ["comedy"])
    for tmdb_id in (1, 2, 3):
        played(tmdb_id, 1)

    def save(key, minute, played_s):
        save_progress(
            conn, username="ann", rating_key=key, media_item_id=key, show_rating_key=None,
            position_s=60.0 * minute, duration_s=6000.0, played_s=played_s,
            now=NOW + timedelta(minutes=minute),
        )  # fmt: skip

    save("4", 10, 600.0)
    save("5", 10, 0.0)
    _taste(conn, "ann", user_id)
    reads = _counting(monkeypatch, taste_mod, "person_plays")

    save("4", 11, 660.0)
    save("5", 11, 60.0)
    _taste(conn, "ann", user_id)
    assert reads == []

    save("5", 12, 360.0)
    _taste(conn, "ann", user_id)
    assert reads == ["person_plays"]


@pytest.mark.parametrize("change", ["play", "rating"])
def test_a_new_play_or_rating_is_read_at_the_next_load(conn, lib, change):
    user_id, played, rated = _person(conn, "ann")
    for tmdb_id in (1, 2, 3, 4, 5, 6):
        lib(tmdb_id, ["comedy"] if tmdb_id <= 3 else ["horror"])
    for tmdb_id in (1, 2, 3, 4):
        played(tmdb_id, 1)
    assert _taste(conn, "ann", user_id)["order"][:2] == ["comedy", "horror"]

    if change == "play":
        played(5, 0)
        played(6, 0)
    else:
        rated(4, 2)

    assert _taste(conn, "ann", user_id)["order"][0] == "horror"


def test_what_is_held_for_one_person_is_never_given_to_another(conn, lib):
    ann, ann_played, _ = _person(conn, "ann")
    bob, bob_played, _ = _person(conn, "bob")
    for tmdb_id in (1, 2, 3, 4, 5, 6):
        lib(tmdb_id, ["comedy"] if tmdb_id <= 3 else ["romance"])
    # The same days for both, so nothing of what each has done tells them apart.
    for days, (a, b) in enumerate(((1, 4), (2, 5), (3, 6))):
        ann_played(a, days)
        bob_played(b, days)

    assert _taste(conn, "ann", ann)["order"][0] == "comedy"
    assert _taste(conn, "bob", bob)["order"][0] == "romance"


@pytest.mark.parametrize(("days_ago", "personal"), [(800, False), (700, True)])
def test_a_play_over_two_years_old_is_not_counted(conn, lib, days_ago, personal):
    user_id, played, _rated = _person(conn, "ann")
    for tmdb_id in (1, 2, 3):
        lib(tmdb_id, ["drama"])
    played(1, 0)
    played(2, 0)
    played(3, days_ago)

    assert _taste(conn, "ann", user_id)["personal"] is personal


def test_more_like_is_the_title_liked_most_lately_not_one_rated_higher_long_ago(conn, lib):
    user_id, played, _rated = _person(conn, "ann")
    for tmdb_id in (1, 2, 3, 4, 5):
        lib(tmdb_id, ["drama"])
        played(tmdb_id, 400)
    played(6, 0)
    lib(6, ["drama"])
    for tmdb_id, level, days in ((4, 2, 365), (5, 1, 0)):
        conn.execute(
            "INSERT INTO user_ratings (user_id, media_type, tmdb_id, level, title, updated_at)"
            " VALUES (?, 'movie', ?, ?, ?, ?)",
            (user_id, tmdb_id, level, f"Film {tmdb_id}", (NOW - timedelta(days=days)).isoformat()),
        )
    conn.commit()

    assert _taste(conn, "ann", user_id)["like"]["ref"] == "tmdb-movie-5"


def test_more_like_carries_the_library_poster_of_a_show_by_its_show_key(conn, lib):
    user_id, played, rated = _person(conn, "ann")
    for tmdb_id in (1, 2, 3):
        lib(tmdb_id, ["drama"])
        played(tmdb_id, 0)
    insert_media_item(
        conn, id="ep-1", plex_rating_key="ep-1", title="Pilot", show_title="Severance",
        show_rating_key="77", media_type="episode", plex_library_id=1,
    )  # fmt: skip
    conn.execute("UPDATE media_items SET tmdb_id = 95396 WHERE id = 'ep-1'")
    conn.execute(
        "INSERT INTO title_meta (id_type, id, tmdb_id, details, cast_json, fetched_at)"
        " VALUES ('tv', 95396, 95396, ?, '[]', '2026-06-01')",
        (
            json.dumps(
                {
                    "title": "Severance", "released": "2022-02-18", "poster": None,
                    "rating": 8.0, "votes": 100, "popularity": 1.0, "genres": ["drama"],
                }
            ),
        ),
    )  # fmt: skip
    conn.execute(
        "INSERT INTO user_ratings (user_id, media_type, tmdb_id, level, title, updated_at)"
        " VALUES (?, 'tv', 95396, 2, 'Severance', ?)",
        (user_id, NOW.isoformat()),
    )
    conn.commit()

    like = _taste(conn, "ann", user_id)["like"]

    assert (like["ref"], like["poster_url"]) == ("tmdb-tv-95396", "/api/poster/77")
