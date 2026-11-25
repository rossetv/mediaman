"""Watched and Dismissed marks: one person's own, undone exactly, only for library titles."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mediaman.services.media_meta.plex_watch import PlexUnavailable
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.session_store import create_session
from mediaman.web.middleware.csrf import CSRFOriginMiddleware
from mediaman.web.repository import ratings, seen_titles
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.watch_progress import (
    continue_watching,
    get_progress,
    save_progress,
    show_progress,
    unmark_watched,
)
from mediaman.web.routes.watch import mark_undo, marks_api, pages
from tests.helpers.factories import insert_admin_user, insert_media_item, insert_settings
from tests.unit.web.watch.test_watch_pages import FakePlex

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
EPISODES = ["511", "512", "521", "522"]


@pytest.fixture
def plex(monkeypatch):
    fake = FakePlex()
    fake.add_movie("100", "Brightwater")
    fake.add_movie("900", "Hidden Film", section="9")
    fake.add_show("500", "The Long Coast", {1: ("501", ["511", "512"]), 2: ("502", ["521", "522"])})
    fake.add_show("700", "Hidden Show", {1: ("701", ["711"])}, section="9")
    monkeypatch.setattr(pages, "PlexWatch", lambda *_a, **_k: fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    times = [NOW]
    monkeypatch.setattr(marks_api, "now_utc", lambda: times[0])
    monkeypatch.setattr(mark_undo, "_held", {})
    return times


def _season(conn, key, show, title, number, library=1):
    insert_media_item(
        conn,
        id=key,
        plex_rating_key=key,
        title=title,
        show_title=title,
        media_type="tv_season",
        season_number=number,
        show_rating_key=show,
        plex_library_id=library,
    )


@pytest.fixture
def as_user(app_factory, conn, plex, clock):
    insert_settings(conn, plex_url="http://plex.invalid:32400", plex_token="tok")
    insert_settings(conn, plex_libraries='["1"]')
    insert_media_item(conn, id="100", plex_rating_key="100", title="Brightwater")
    insert_media_item(conn, id="900", plex_rating_key="900", title="Hidden Film", plex_library_id=9)
    _season(conn, "501", "500", "The Long Coast", 1)
    _season(conn, "502", "500", "The Long Coast", 2)
    _season(conn, "701", "700", "Hidden Show", 1, library=9)
    app = app_factory(marks_api.router, conn=conn)

    def _client(username):
        client = TestClient(app)
        client.cookies.set("session_token", create_session(conn, username))
        return client

    for username in ("aoife", "cian"):
        insert_admin_user(conn, username=username, role="viewer")
    return _client


@pytest.fixture
def client(as_user):
    return as_user("aoife")


def _seen(conn, keys, username="aoife"):
    found = get_progress(conn, username, keys)
    return [k for k in keys if k in found and found[k].seen]


def _rows(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM watch_progress ORDER BY rating_key")]


def _play(
    conn, key, *, position, duration=3000.0, item="501", show="500", now=NOW - timedelta(hours=1)
):
    save_progress(
        conn,
        username="aoife",
        rating_key=key,
        media_item_id=item,
        show_rating_key=show,
        position_s=position,
        duration_s=duration,
        played_s=position,
        now=now,
    )


@pytest.mark.parametrize(
    ("kind", "key", "covered"),
    [
        ("movie", "100", ["100"]),
        ("episode", "512", ["512"]),
        ("season", "502", ["521", "522"]),
        ("show", "500", EPISODES),
    ],
)
def test_marking_and_unmarking_cover_a_title_whole_and_no_other(as_user, conn, kind, key, covered):
    client, every = as_user("aoife"), ["100", *EPISODES]

    marked = client.post(f"/api/watch/watched/{kind}/{key}")
    assert marked.json() == {"ok": True, "watched": True, "changed": len(covered)}
    assert _seen(conn, every) == covered
    assert client.post(f"/api/watch/watched/{kind}/{key}").json()["changed"] == 0
    assert as_user("cian").delete(f"/api/watch/watched/{kind}/{key}").json()["changed"] == 0
    assert (_seen(conn, every), _seen(conn, every, "cian")) == (covered, [])

    unmarked = client.delete(f"/api/watch/watched/{kind}/{key}")
    assert unmarked.json() == {"ok": True, "watched": False, "changed": len(covered)}
    assert _seen(conn, every) == []


def test_an_episode_mark_belongs_to_its_season_and_show(client, conn):
    client.post("/api/watch/watched/season/502")
    rows = conn.execute("SELECT DISTINCT media_item_id, show_rating_key FROM watch_progress")
    assert [tuple(r) for r in rows] == [("502", "500")]


def _title_marks(conn, username="aoife"):
    return seen_titles.person_marks(conn, username).seen


def _covered(conn, every=EPISODES):
    """The episodes that read as seen, by rows and by the show's mark."""
    held = show_progress(conn, "aoife", "500")
    read = seen_titles.with_covered(conn, "aoife", ["1"], "500", held)
    return [key for key in every if key in read and read[key].seen]


def _watched(conn):
    return lib.seen_refs(conn, "aoife", ["1"])


@pytest.fixture
def matched(conn):
    """The library's titles with their TMDB ids, and the episodes a scan listed."""
    conn.execute("UPDATE media_items SET tmdb_id = 603 WHERE id = '100'")
    conn.execute("UPDATE media_items SET tmdb_id = 44 WHERE show_rating_key = '500'")
    for season in ("501", "502"):
        keys = [f"5{season[2]}{n}" for n in (1, 2)]
        listed = json.dumps([{"key": k, "number": int(k[2]), "path": k} for k in keys])
        conn.execute(
            "UPDATE media_items SET episode_count = 2, episode_keys = ? WHERE id = ?",
            (listed, season),
        )
    conn.commit()


@pytest.fixture
def joined(conn, as_user, matched):
    """A show aoife marked while the library held none of it."""
    _mark_outside(conn)


def _mark_outside(conn):
    user_id = ratings.find_user_id(conn, "aoife")
    title = ratings.RatedTitle("tv", 44)
    seen_titles.mark(conn, user_id, title, 2, NOW, in_library=False)


class TestTheTitlesOwnMark:
    def test_a_film_and_a_show_are_marked_as_titles_and_taken_back_so(self, as_user, conn, matched):
        client = as_user("aoife")
        client.post("/api/watch/watched/movie/100")
        client.post("/api/watch/watched/show/500")
        assert _title_marks(conn) == {("movie", 603): None, ("tv", 44): 2}
        assert _title_marks(conn, "cian") == {}

        client.delete("/api/watch/watched/movie/100")
        client.delete("/api/watch/watched/show/500")

        assert (_title_marks(conn), _watched(conn)) == ({}, set())

    @pytest.mark.parametrize(("kind", "key"), [("episode", "511"), ("season", "501")])
    def test_a_part_of_a_show_marks_no_title(self, client, conn, matched, kind, key):
        client.post(f"/api/watch/watched/{kind}/{key}")
        assert _title_marks(conn) == {}

    def test_a_title_with_no_tmdb_id_is_marked_as_before(self, client, conn):
        assert client.post("/api/watch/watched/movie/100").json()["changed"] == 1
        assert _title_marks(conn) == {}

    @pytest.mark.parametrize(
        ("kind", "key", "title"), [("movie", "100", ("movie", 603)), ("show", "500", ("tv", 44))]
    )
    def test_undo_puts_the_titles_mark_back_too(self, client, conn, matched, kind, key, title):
        path = f"/api/watch/watched/{kind}/{key}"
        client.post(path)
        client.delete(f"{path}?undo=1")
        assert (_title_marks(conn), _watched(conn)) == ({}, set())

        client.post(path)
        client.delete(path)
        client.post(f"{path}?undo=1")
        assert (list(_title_marks(conn)), _watched(conn)) == ([title], {f"{kind}-{key}"})

    def test_undo_marks_nothing_for_a_new_account_under_the_name(self, as_user, conn, matched):
        path = "/api/watch/watched/movie/100"
        client = as_user("aoife")
        client.post(path)
        client.delete(path)
        conn.execute("DELETE FROM admin_users WHERE username = 'aoife'")
        conn.commit()
        insert_admin_user(conn, username="aoife", role="viewer")

        assert as_user("aoife").post(f"{path}?undo=1").status_code == 409
        assert _title_marks(conn) == {}

    def test_marking_twice_leaves_the_first_undo(self, client, conn, matched):
        for _ in range(2):
            client.post("/api/watch/watched/movie/100")
        client.delete("/api/watch/watched/movie/100?undo=1")
        assert (_title_marks(conn), _watched(conn)) == ({}, set())


class TestAShowMarkedBeforeItJoined:
    def test_it_is_taken_back_whole_and_undone(self, client, conn, joined):
        assert _watched(conn) == {"show-500"}

        taken = client.delete("/api/watch/watched/show/500").json()

        assert (taken["changed"], _watched(conn), _title_marks(conn)) == (1, set(), {})
        client.post("/api/watch/watched/show/500?undo=1")
        assert (_watched(conn), _rows(conn)) == ({"show-500"}, [])

    @pytest.mark.parametrize(
        ("kind", "key", "rest"),
        [("episode", "512", ["511", "521", "522"]), ("season", "501", ["521", "522"])],
    )
    def test_taking_a_part_back_leaves_the_rest_and_its_undo_leaves_no_trace(
        self, client, conn, joined, kind, key, rest
    ):
        path = f"/api/watch/watched/{kind}/{key}"

        assert client.delete(path).json()["changed"] == 1

        assert (_covered(conn), _watched(conn), _rows(conn)) == (rest, set(), [])
        client.post(f"{path}?undo=1")
        assert (_watched(conn), _rows(conn)) == ({"show-500"}, [])

    @pytest.mark.parametrize(("kind", "key"), [("episode", "512"), ("season", "501")])
    def test_a_part_taken_back_and_marked_again_is_watched_wherever_the_show_is(
        self, client, conn, joined, kind, key
    ):
        client.delete(f"/api/watch/watched/{kind}/{key}")
        assert seen_titles.person_marks(conn, "aoife").partly == {("tv", 44)}

        client.post(f"/api/watch/watched/{kind}/{key}")

        assert _watched(conn) == {"show-500"}
        assert seen_titles.person_marks(conn, "aoife").partly == set()

    def test_marking_a_season_again_gives_back_the_episodes_taken_back(self, client, conn, joined):
        client.delete("/api/watch/watched/episode/512")
        client.post("/api/watch/watched/season/501")

        assert seen_titles.person_marks(conn, "aoife").partly == set()

    def test_taking_a_part_back_twice_leaves_the_first_undo(self, client, conn, joined):
        path = "/api/watch/watched/season/501"
        assert [client.delete(path).json()["changed"] for _ in range(2)] == [1, 0]
        client.post("/api/watch/watched/season/501?undo=1")

        assert _watched(conn) == {"show-500"}

    def test_seasons_taken_back_one_after_the_other_both_stay_so(self, client, conn, joined):
        client.delete("/api/watch/watched/season/501")
        client.delete("/api/watch/watched/season/502")
        assert _covered(conn) == []

        assert client.post("/api/watch/watched/season/501?undo=1").status_code == 409
        assert _covered(conn) == []

    @pytest.mark.parametrize("between", ["season/501", "episode/511"])
    def test_a_mark_made_since_stands_against_an_older_undo(self, client, conn, joined, between):
        client.delete("/api/watch/watched/show/500")
        client.post(f"/api/watch/watched/{between}")
        client.delete(f"/api/watch/watched/{between}")

        assert client.post("/api/watch/watched/show/500?undo=1").status_code == 409
        assert _covered(conn) == []

    def test_a_season_with_no_episode_list_yet_stays_covered(self, client, conn, joined):
        conn.execute("UPDATE media_items SET episode_keys = NULL WHERE id = '502'")
        conn.commit()
        client.delete("/api/watch/watched/season/501")

        show = lib.show_seen(conn, "aoife", ["1"])["500"]
        assert [show.seasons[n].watched for n in (1, 2)] == [False, True]

    def test_an_episode_the_mark_never_reached_leaves_it(self, client, conn, joined):
        _season(conn, "503", "500", "The Long Coast", 3)
        conn.execute(
            "UPDATE media_items SET tmdb_id = 44, episode_keys = ? WHERE id = '503'",
            (json.dumps([{"key": "531", "number": 1, "path": "531"}]),),
        )
        _play(conn, "531", position=2900, item="503")

        client.delete("/api/watch/watched/episode/531")

        mark = seen_titles.get_mark(conn, ratings.find_user_id(conn, "aoife"), ("tv", 44))
        assert (mark.through_season, mark.partly) == (2, False)
        assert _covered(conn) == EPISODES

    def test_what_was_begun_again_stays_begun_when_another_part_is_taken_back(
        self, client, conn, joined
    ):
        _play(conn, "521", position=900, item="502")
        before = _rows(conn)
        client.delete("/api/watch/watched/episode/511")

        assert (_covered(conn), _rows(conn)) == (["512", "522"], before)

    def test_marking_it_again_in_the_library_and_undoing_it_leaves_it_as_it_was(
        self, client, conn, joined
    ):
        client.post("/api/watch/watched/show/500")
        client.delete("/api/watch/watched/show/500?undo=1")

        assert (_watched(conn), _title_marks(conn)) == ({"show-500"}, {("tv", 44): 2})
        assert _rows(conn) == []

    def test_marking_all_of_it_taken_back_is_undone_to_what_was_marked_in_the_library(
        self, client, conn, matched
    ):
        client.post("/api/watch/watched/show/500")
        client.delete("/api/watch/watched/season/501")
        assert _covered(conn) == ["521", "522"]
        _season(conn, "503", "500", "The Long Coast", 3)

        client.post("/api/watch/watched/show/500")
        client.delete("/api/watch/watched/show/500?undo=1")

        assert _covered(conn) == ["521", "522"]

    def test_a_mark_taken_back_on_a_special_leaves_the_show(self, client, conn, joined, plex):
        _season(conn, "509", "500", "The Long Coast", 0)
        client.delete("/api/watch/watched/season/509")
        assert seen_titles.person_marks(conn, "aoife").partly == set()


def _arrives(conn, plex, key="523", season="502"):
    """An episode more, as a scan lists it and Plex names it."""
    stored = conn.execute("SELECT episode_keys FROM media_items WHERE id = ?", (season,))
    listed = json.loads(stored.fetchone()[0])
    listed.append({"key": key, "number": len(listed) + 1, "path": key})
    conn.execute(
        "UPDATE media_items SET episode_keys = ?, episode_count = ? WHERE id = ?",
        (json.dumps(listed), len(listed), season),
    )
    conn.commit()
    number = int(season[2])
    plex.add_show(
        "500",
        "The Long Coast",
        {1: ("501", ["511", "512"]), number: (season, [e["key"] for e in listed])},
    )


class TestMarkedInTheLibrary:
    @pytest.mark.parametrize("played", [None, NOW - timedelta(days=1), NOW + timedelta(days=1)])
    @pytest.mark.parametrize("again", [False, True])
    def test_an_episode_that_comes_later_is_not_seen_whatever_was_taken_back(
        self, client, conn, matched, plex, played, again
    ):
        def play():
            for key in ("521", "522"):
                _play(conn, key, position=2900, item="502", now=played)

        if played and played < NOW:
            play()
        client.post("/api/watch/watched/show/500")
        if played and played > NOW:
            play()
        for key in ("521", "522"):
            client.delete(f"/api/watch/watched/episode/{key}")
        if again:
            client.post("/api/watch/watched/season/502")
            client.delete("/api/watch/watched/season/502?undo=1")

        _arrives(conn, plex)

        assert _covered(conn, [*EPISODES, "523"]) == ["511", "512"]

    def test_a_film_unmarked_by_an_older_version_is_so_after_a_mark_is_undone(
        self, client, conn, matched
    ):
        path = "/api/watch/watched/movie/100"
        client.post(path)
        unmark_watched(conn, "aoife", "movie", "100")
        client.post(path)

        client.delete(f"{path}?undo=1")

        assert _watched(conn) == set()


class TestWhatIsLeftOfAMark:
    def test_an_episode_that_comes_after_a_season_was_taken_back_is_not_seen(
        self, client, conn, joined, plex
    ):
        client.delete("/api/watch/watched/season/502")
        client.post("/api/watch/watched/episode/521")

        _arrives(conn, plex)

        assert _covered(conn, [*EPISODES, "523"]) == ["511", "512", "521"]

    def test_a_show_held_from_its_second_season_keeps_no_mark_once_taken_back(
        self, client, conn, matched, plex
    ):
        conn.execute("DELETE FROM media_items WHERE id = '501'")
        conn.commit()
        plex.add_show("500", "The Long Coast", {2: ("502", ["521", "522"])})
        client.post("/api/watch/watched/show/500")

        client.delete("/api/watch/watched/season/502")

        assert _title_marks(conn) == {}

    def test_an_episode_gone_since_the_mark_keeps_nothing_of_it(self, client, conn, matched):
        client.post("/api/watch/watched/show/500")
        conn.execute(
            "UPDATE media_items SET episode_keys = ?, episode_count = 1 WHERE id = '502'",
            (json.dumps([{"key": "521", "number": 1, "path": "p"}]),),
        )
        conn.commit()
        for part in ("season/501", "episode/521"):
            client.delete(f"/api/watch/watched/{part}")

        assert _title_marks(conn) == {}

    def test_a_season_not_listed_is_taken_back_and_given_back(self, client, conn, joined):
        conn.execute("UPDATE media_items SET episode_keys = NULL WHERE id = '502'")
        conn.commit()

        def watched():
            return lib.show_seen(conn, "aoife", ["1"])["500"].seasons[2].watched

        client.delete("/api/watch/watched/season/502")
        assert not watched()
        client.post("/api/watch/watched/season/502")
        conn.execute("DELETE FROM watch_progress")
        conn.commit()
        assert watched()

    def test_with_one_of_two_parts_given_back_the_show_is_still_not_whole(
        self, client, conn, joined
    ):
        for part in ("season/501", "episode/521"):
            client.delete(f"/api/watch/watched/{part}")
        client.post("/api/watch/watched/season/501")

        assert seen_titles.person_marks(conn, "aoife").partly == {("tv", 44)}

    def test_an_episode_gone_since_it_was_taken_back_leaves_the_show_whole(
        self, client, conn, joined, plex
    ):
        _arrives(conn, plex)
        client.delete("/api/watch/watched/episode/523")
        conn.execute(
            "UPDATE media_items SET episode_keys = ?, episode_count = 2 WHERE id = '502'",
            (json.dumps([{"key": f"52{n}", "number": n, "path": "p"} for n in (1, 2)]),),
        )
        conn.commit()
        plex.add_show(
            "500", "The Long Coast", {1: ("501", ["511", "512"]), 2: ("502", ["521", "522"])}
        )
        client.post("/api/watch/watched/season/502")

        assert seen_titles.person_marks(conn, "aoife").partly == set()
        assert _watched(conn) == {"show-500"}

    def test_an_episode_with_no_number_speaks_for_itself_alone(self, client, conn, joined, plex):
        unnumbered = [{"key": "511", "number": None, "path": "a"}]
        unnumbered.append({"key": "512", "number": 2, "path": "b"})
        conn.execute(
            "UPDATE media_items SET episode_keys = ? WHERE id = '501'", (json.dumps(unnumbered),)
        )
        conn.commit()
        assert _covered(conn) == ["512", "521", "522"]

        client.post("/api/watch/watched/episode/511")
        assert _covered(conn) == EPISODES
        client.delete("/api/watch/watched/episode/511")
        assert _covered(conn) == ["512", "521", "522"]

        client.delete("/api/watch/watched/season/501")
        client.post("/api/watch/watched/episode/511")
        assert _covered(conn) == ["511", "521", "522"]

    @pytest.mark.parametrize(
        "parts",
        [
            ["season/501", "season/502"],
            ["episode/511", "episode/512", "season/502"],
            [f"episode/{e}" for e in EPISODES],
        ],
    )
    @pytest.mark.parametrize("start", ["joined", "in the library"])
    def test_with_every_part_taken_back_the_mark_is_gone_and_undone_to_the_last(
        self, client, conn, matched, start, parts
    ):
        if start == "joined":
            _mark_outside(conn)
        else:
            client.post("/api/watch/watched/show/500")
        for part in parts:
            client.delete(f"/api/watch/watched/{part}")
        assert _title_marks(conn) == {}

        client.post(f"/api/watch/watched/{parts[-1]}?undo=1")

        assert _title_marks(conn) == {("tv", 44): 2}

    def test_an_episode_given_back_of_a_season_taken_back_comes_alone(self, client, conn, joined):
        client.delete("/api/watch/watched/season/501")
        client.post("/api/watch/watched/episode/511")
        assert _covered(conn) == ["511", "521", "522"]

        client.post("/api/watch/watched/episode/512")

        assert seen_titles.person_marks(conn, "aoife").partly == set()


@pytest.mark.parametrize("start", ["joined", "nothing"])
def test_an_older_undo_takes_nothing_back_of_a_whole_show_marked_since(
    client, conn, matched, start
):
    if start == "joined":
        _mark_outside(conn)
    client.post("/api/watch/watched/episode/511")
    client.post("/api/watch/watched/show/500")

    assert client.delete("/api/watch/watched/episode/511?undo=1").status_code == 409
    assert _watched(conn) == {"show-500"}


def test_marks_of_two_episodes_of_a_show_not_marked_are_each_undone(client, conn, matched):
    for key in ("511", "512"):
        client.post(f"/api/watch/watched/episode/{key}")
    for key in ("511", "512"):
        assert client.delete(f"/api/watch/watched/episode/{key}?undo=1").status_code == 200

    assert _covered(conn) == []


_OF = {
    "show/500": set(EPISODES),
    "season/501": {"511", "512"},
    "season/502": {"521", "522"},
    **{f"episode/{e}": {e} for e in EPISODES},
}


def test_marking_a_show_watched_leaves_no_episode_of_it_unseen(client, conn, matched, plex):
    """Not one whose own mark was taken back before the show was marked elsewhere."""
    client.post("/api/watch/watched/episode/511")
    client.delete("/api/watch/watched/episode/511")
    _mark_outside(conn)
    _season(conn, "503", "500", "The Long Coast", 3)
    seasons = {n: (f"50{n}", [f"5{n}1", f"5{n}2"]) for n in (1, 2, 3)}
    plex.add_show("500", "The Long Coast", seasons)

    client.post("/api/watch/watched/show/500")

    assert _covered(conn) == EPISODES


@pytest.fixture
def on_the_page(app_factory, conn, client, templates_stub, monkeypatch):
    """The key of the episode that the Watch page offers to go on with."""
    monkeypatch.setattr(marks_api, "_MARK_LIMITER", ActionRateLimiter(10**6, 60))
    monkeypatch.setattr(pages, "_reachable_until", [float("inf")])
    app = app_factory(pages.router, conn=conn, state_extras={"templates": templates_stub})
    page = TestClient(app)
    page.cookies.set("session_token", create_session(conn, "aoife"))

    def _read():
        rows = page.get("/watch").json()["rows"]
        items = next((r["items"] for r in rows if r["id"] == "continue"), [])
        return items[0]["mark_key"] if items else None

    return _read


@pytest.mark.parametrize("start", ["joined", "back in the library"])
def test_the_episode_to_go_on_with_is_marked_and_the_next_one_comes(
    client, conn, matched, on_the_page, start
):
    _begin(client, conn, start)
    _play(conn, "511", position=2900)
    assert on_the_page() == "512"

    client.post("/api/watch/watched/episode/512")
    assert on_the_page() == "521"

    client.delete("/api/watch/watched/episode/512?undo=1")
    assert on_the_page() == "512"
    assert _covered(conn) == EPISODES


def _begin(client, conn, start):
    if start == "joined":
        _mark_outside(conn)
    elif start != "nothing":
        client.post("/api/watch/watched/show/500")
    if start == "back in the library":
        conn.execute("DELETE FROM watch_progress")
        conn.commit()


@pytest.mark.parametrize(
    "start", ["nothing", "joined", "in the library", "back in the library", "played"]
)
def test_marks_in_any_order_read_as_they_were_made_and_undo_exactly(
    client, conn, matched, on_the_page, start
):
    """What a person sees: the episodes, the show, and the one to go on with."""
    dice = random.Random(7)
    for _ in range(25):
        conn.execute("DELETE FROM watch_progress")
        conn.execute("DELETE FROM user_seen_titles")
        conn.commit()
        mark_undo._held.clear()
        _begin(client, conn, "joined" if start == "played" else start)
        seen = set() if start == "nothing" else set(EPISODES)
        if start == "played":
            _play(conn, "511", position=2900)
        made = []
        for _ in range(4):
            part, marks = dice.choice(list(_OF)), dice.random() < 0.5
            made.append((part, marks))
            path = f"/api/watch/watched/{part}"
            before, offered = seen, on_the_page()
            seen = seen | _OF[part] if marks else seen - _OF[part]
            changed = (client.post if marks else client.delete)(path).json()["changed"]
            assert _covered(conn) == sorted(seen), made
            assert not (marks and on_the_page() in _OF[part]), made
            if changed and dice.random() < 0.4:
                (client.delete if marks else client.post)(f"{path}?undo=1")
                seen = before
                assert (_covered(conn), on_the_page()) == (sorted(seen), offered), made
        assert bool(_watched(conn)) is (seen == set(EPISODES)), made


class TestCopiesOfAFilm:
    @pytest.fixture
    def copies(self, conn, as_user, matched):
        insert_media_item(conn, id="102", plex_rating_key="102", title="Brightwater 4K")
        conn.execute("UPDATE media_items SET tmdb_id = 603 WHERE id = '102'")
        conn.commit()

    @pytest.mark.parametrize("key", ["100", "102"])
    def test_taking_the_mark_back_on_either_takes_it_off_both_and_is_undone(
        self, client, conn, copies, key
    ):
        client.post("/api/watch/watched/movie/100")
        assert _watched(conn) == {"movie-100", "movie-102"}

        client.delete(f"/api/watch/watched/movie/{key}")
        assert _watched(conn) == set()

        client.post(f"/api/watch/watched/movie/{key}?undo=1")
        assert _watched(conn) == {"movie-100", "movie-102"}

    def test_marking_a_copy_that_is_watched_already_leaves_nothing_to_undo(
        self, client, conn, copies
    ):
        client.post("/api/watch/watched/movie/100")
        client.post("/api/watch/watched/movie/102")
        client.delete("/api/watch/watched/movie/102?undo=1")

        assert _watched(conn) == {"movie-100", "movie-102"}

    def test_a_row_unmarked_by_an_older_version_stays_so(self, client, conn, copies):
        client.post("/api/watch/watched/movie/100")
        unmark_watched(conn, "aoife", "movie", "100")

        assert "movie-100" not in _watched(conn)


class TestUndo:
    def test_undoing_a_show_mark_restores_only_what_it_changed(self, client, conn):
        _play(conn, "511", position=3000)
        _play(conn, "512", position=900)
        before = _rows(conn)
        client.post("/api/watch/watched/show/500")

        undone = client.delete("/api/watch/watched/show/500?undo=1")

        assert undone.json() == {"ok": True, "watched": False, "changed": 3}
        assert [r for r in _rows(conn) if r[1] in ("511", "512")] == before
        assert _seen(conn, EPISODES) == ["511"]

    def test_undoing_an_unmark_brings_the_mark_and_the_place_back(self, client, conn):
        _play(conn, "511", position=2900)
        before = _rows(conn)
        client.delete("/api/watch/watched/episode/511")

        assert client.post("/api/watch/watched/episode/511?undo=1").json()["watched"] is True
        assert _rows(conn) == before

    def test_nothing_to_undo_changes_nothing(self, as_user, conn, clock):
        client = as_user("aoife")
        client.post("/api/watch/watched/movie/100")

        others = as_user("cian").delete("/api/watch/watched/movie/100?undo=1")
        wrong_way = client.post("/api/watch/watched/movie/100?undo=1")
        clock[0] = NOW + mark_undo.UNDO_WINDOW
        too_late = client.delete("/api/watch/watched/movie/100?undo=1")

        for resp in (others, wrong_way, too_late):
            assert (resp.status_code, resp.json()["error"]) == (409, "too_late")
        assert _seen(conn, ["100"]) == ["100"]

    def test_an_undo_works_once(self, client):
        client.post("/api/watch/watched/movie/100")
        assert client.delete("/api/watch/watched/movie/100?undo=1").status_code == 200
        assert client.delete("/api/watch/watched/movie/100?undo=1").status_code == 409

    def test_marking_twice_keeps_the_first_marks_undo(self, client, conn):
        client.post("/api/watch/watched/movie/100")
        client.post("/api/watch/watched/movie/100")
        assert client.delete("/api/watch/watched/movie/100?undo=1").status_code == 200
        assert _seen(conn, ["100"]) == []


class TestRefusals:
    @pytest.mark.parametrize(
        "path",
        [
            "watched/movie/900",
            "watched/movie/511",
            "watched/episode/711",
            "watched/episode/100",
            "watched/season/701",
            "watched/season/500",
            "watched/season/404",
            "watched/show/700",
            "watched/show/501",
            "watched/trailer/100",
            "watched/movie/abc",
            "dismissed/movie-900",
            "dismissed/show-700",
            "dismissed/show-100",
            "dismissed/tmdb-movie-1",
            "dismissed/movie-",
        ],
    )
    def test_what_is_not_in_the_scanned_libraries_is_not_found(self, client, conn, path):
        resp = client.post(f"/api/watch/{path}")
        assert (resp.status_code, resp.json()["error"]) == (404, "not_found")
        assert _rows(conn) == []

    @pytest.mark.parametrize("path", ["episode/511", "season/501", "show/500"])
    def test_a_title_plex_has_moved_out_of_the_scanned_libraries_is_not_found(
        self, client, conn, plex, path
    ):
        plex.add_show("500", "The Long Coast", {1: ("501", ["511", "512"])}, section="9")
        assert client.post(f"/api/watch/watched/{path}").status_code == 404
        assert _rows(conn) == []

    def test_progress_of_a_library_no_longer_scanned_cannot_be_unmarked(self, client, conn):
        _play(conn, "711", position=3000, item="701", show="700")
        for path in ("episode/711", "season/701", "show/700"):
            assert client.delete(f"/api/watch/watched/{path}").status_code == 404
        assert _seen(conn, ["711"]) == ["711"]

    def test_plex_failing_part_way_marks_nothing(self, client, conn, plex, monkeypatch):
        real = plex.get_season_episodes

        def fail_second(key):
            if key == "502":
                raise PlexUnavailable("down")
            return real(key)

        monkeypatch.setattr(plex, "get_season_episodes", fail_second)
        resp = client.post("/api/watch/watched/show/500")
        assert (resp.status_code, resp.json()["error"]) == (503, "unreachable")
        assert _rows(conn) == []

    def test_a_film_is_marked_and_a_show_unmarked_with_plex_down(self, client, conn, plex):
        client.post("/api/watch/watched/show/500")
        plex.error = PlexUnavailable("down")
        assert client.delete("/api/watch/watched/show/500").json()["changed"] == 4
        assert client.post("/api/watch/watched/movie/100").json()["changed"] == 1

    @pytest.mark.parametrize("path", ["watched/movie/100", "dismissed/movie-100"])
    @pytest.mark.parametrize("method", ["POST", "DELETE"])
    def test_changes_are_rate_limited_per_person(self, client, monkeypatch, method, path):
        monkeypatch.setattr(marks_api._MARK_LIMITER, "check", lambda _key: False)
        resp = client.request(method, f"/api/watch/{path}")
        assert (resp.status_code, resp.json()["error"]) == (429, "too_many_requests")

    @pytest.mark.parametrize("path", ["watched/movie/100", "dismissed/movie-100"])
    def test_a_signed_out_caller_changes_nothing(self, as_user, conn, path):
        client = as_user("aoife")
        client.cookies.clear()
        assert client.post(f"/api/watch/{path}").status_code == 401
        assert _rows(conn) == []


@pytest.mark.parametrize("path", ["watched/movie/100", "dismissed/movie-100"])
@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_another_sites_request_changes_nothing(client, conn, method, path):
    client.app.add_middleware(CSRFOriginMiddleware)
    _play(conn, "100", position=900, item="100", show=None)
    before = _rows(conn)

    resp = client.request(
        method, f"/api/watch/{path}", headers={"origin": "https://elsewhere.example"}
    )

    assert resp.status_code == 403
    assert _rows(conn) == before


class TestDismiss:
    @pytest.mark.parametrize(
        ("ref", "key", "item", "show"),
        [("movie-100", "100", "100", None), ("show-500", "511", "501", "500")],
    )
    def test_dismissing_hides_the_title_and_undo_brings_it_back(
        self, client, conn, ref, key, item, show
    ):
        _play(conn, key, position=900, item=item, show=show)
        before = _rows(conn)

        assert client.post(f"/api/watch/dismissed/{ref}").json() == {"ok": True}
        assert continue_watching(conn, "aoife") == []
        assert client.delete(f"/api/watch/dismissed/{ref}").json() == {"ok": True}

        assert [c.progress.rating_key for c in continue_watching(conn, "aoife")] == [key]
        assert _rows(conn) == before
