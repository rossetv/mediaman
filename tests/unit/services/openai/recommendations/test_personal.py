"""Each person's own picks: made from their plays alone, stored as theirs alone."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import requests

from mediaman.db import init_db
from mediaman.scanner.repository.media_items import delete_media_items
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.openai.recommendations import personal, prompts, user_picks
from mediaman.services.openai.recommendations.persist import refresh_recommendations
from mediaman.services.openai.recommendations.plays import person_plays
from mediaman.services.openai.recommendations.user_picks import RefreshState
from mediaman.web.repository.watch_progress import save_progress
from tests.helpers.factories import (
    insert_media_item,
    insert_settings,
    insert_suggestion,
    insert_user_pick,
)

_KEY = "0123456789abcdef" * 4
NOW = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)


def _person(conn, username):
    # A stand-in hash: a real one takes a third of a second.
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, created_at, role)"
        " VALUES (?, 'x', ?, 'viewer')",
        (username, _ago(days=100)),
    )
    conn.commit()


def _ago(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat()


@pytest.fixture
def conn(db_path, freezer):
    freezer.move_to(NOW)
    conn = init_db(str(db_path))
    for name in ("amy", "bob"):
        _person(conn, name)
    insert_settings(conn, openai_api_key="sk-test")
    yield conn
    conn.close()


@pytest.fixture
def openai(monkeypatch):
    """Answers every call with ``items`` (or raises ``error``); ``prompts`` are what was sent."""
    fake = MagicMock(prompts=[], items=[], error=None)

    def call(prompt, conn, use_web_search=False, *, secret_key=None):
        fake.prompts.append(prompt)
        if fake.error:
            raise fake.error
        return fake.items

    monkeypatch.setattr(prompts, "call_openai", call)
    monkeypatch.setattr("mediaman.services.media_meta.tmdb.TmdbClient.from_db", lambda *_a: None)
    monkeypatch.setattr("mediaman.services.media_meta.omdb.fetch_ratings", lambda *_a, **_k: {})
    return fake


def _pick(title: str, media_type: str = "movie", **extra) -> dict:
    return {"title": title, "media_type": media_type, "reason": f"A story of {title}.", **extra}


def _play(conn, username, title, when=None, *, show=None, movie_key=None, **row):
    """*username* plays a movie *title*, or an episode of the show *title* with key *show*."""
    key = movie_key or f"k{conn.execute('SELECT COUNT(*) FROM watch_progress').fetchone()[0]}"
    item = f"i-{show or title}"
    if not conn.execute("SELECT 1 FROM media_items WHERE id = ?", (item,)).fetchone():
        insert_media_item(
            conn,
            id=item,
            title=title,
            media_type="tv_season" if show else "movie",
            show_title=title if show else None,
            show_rating_key=show,
            plex_rating_key=item,
        )
    values = {
        "username": username,
        "rating_key": key,
        "media_item_id": item,
        "show_rating_key": show,
        "watched": 1,
        "updated_at": when or _ago(days=1),
        **row,
    }
    conn.execute(
        f"INSERT INTO watch_progress ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
        tuple(values.values()),
    )
    conn.commit()


def _plays(conn, username, *titles, **kwargs):
    for title in titles:
        _play(conn, username, title, **kwargs)


def _link(conn, username, account=7):
    conn.execute(
        "INSERT INTO plex_links (username, plex_account_id, server_account_id, plex_name,"
        " server_id, linked_at) VALUES (?, ?, ?, 'P', 's', ?)",
        (username, account, account, _ago(days=60)),
    )
    conn.commit()


def _plex_play(conn, username, title, when, *, show_title=None):
    conn.execute(
        "INSERT INTO plex_plays (username, history_key, media_type, title, show_title, viewed_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            username,
            f"/h/{title}/{when}",
            "episode" if show_title else "movie",
            title,
            show_title,
            when,
        ),
    )
    conn.commit()


def _make(conn, username, plex=None) -> int:
    run = user_picks.start_run(conn, username, "scan", NOW)
    return personal.make_picks(conn, username, plex, run, secret_key=_KEY)


def _titles(conn, username) -> list[str]:
    return [r["title"] for r in user_picks.fetch_picks(conn, username)]


class TestPlays:
    def test_only_the_persons_own_plays_newest_first_a_show_once(self, conn):
        _play(conn, "amy", "Old Film", _ago(days=9))
        _play(conn, "amy", "Show", _ago(days=8), show="s1")
        _play(conn, "amy", "Show", _ago(days=2), show="s1")
        _play(conn, "bob", "Bobs Film", _ago(days=1))

        assert [(p.kind, p.title) for p in person_plays(conn, "amy")] == [
            ("tv", "Show"),
            ("movie", "Old Film"),
        ]

    def test_a_mark_or_a_glance_is_not_a_play_and_a_credited_one_is(self, conn):
        _play(conn, "amy", "Marked", "1970-01-01T00:00:00+00:00", seen_at=_ago(days=1))
        _play(conn, "amy", "Glanced", watched=0)
        _play(conn, "amy", "Credited", watched=0, credited_at=_ago(days=1))
        _play(conn, "amy", "Begun again", watched=0, seen_at=_ago(days=5))

        assert {p.title for p in person_plays(conn, "amy")} == {"Credited", "Begun again"}

    def test_plex_plays_count_for_their_owner_and_merge_with_those_here(self, conn):
        _link(conn, "amy")
        _play(conn, "amy", "Film", _ago(days=5))
        _plex_play(conn, "amy", "Film", _ago(days=1))
        _plex_play(conn, "amy", "Pilot", _ago(days=2), show_title="Plex Show")

        assert [(p.kind, p.title, p.in_plex) for p in person_plays(conn, "amy")] == [
            ("movie", "Film", True),
            ("tv", "Plex Show", True),
        ]
        assert person_plays(conn, "bob") == []

    def _watch(self, conn, username, item, *, show=None, played_s=600.0, key=None):
        save_progress(
            conn,
            username=username,
            rating_key=key or item,
            media_item_id=item,
            show_rating_key=show,
            position_s=600,
            duration_s=3600,
            played_s=played_s,
            now=NOW,
        )

    def test_a_credited_play_outlives_its_title_and_goes_with_the_person(self, conn):
        insert_media_item(conn, id="f", title="Film", plex_rating_key="f")
        conn.execute("UPDATE media_items SET tmdb_id = 7")
        insert_media_item(conn, id="g", title="Glanced at", plex_rating_key="g")
        season = {"media_type": "tv_season", "show_rating_key": "s", "plex_rating_key": "s1"}
        insert_media_item(conn, id="s1", title="Show S1", show_title="Show", **season)
        self._watch(conn, "amy", "f")
        self._watch(conn, "amy", "g", played_s=20.0)
        self._watch(conn, "amy", "s1", show="s", key="e1")
        self._watch(conn, "amy", "not-scanned-yet", show="s", key="e2")
        before = person_plays(conn, "amy")

        delete_media_items(conn, ["f", "g", "s1"])
        conn.execute("DELETE FROM watch_progress")
        conn.commit()

        assert {(p.kind, p.title, p.tmdb_id) for p in before} == {
            ("movie", "Film", 7),
            ("tv", "Show", None),
        }
        assert set(person_plays(conn, "amy")) == set(before)
        assert person_plays(conn, "bob") == []
        conn.execute("DELETE FROM admin_users WHERE username = 'amy'")
        _person(conn, "amy")
        assert person_plays(conn, "amy") == []

    def test_a_title_renamed_since_it_was_played_counts_once(self, conn):
        insert_media_item(conn, id="f", title="Old Name", plex_rating_key="f")
        conn.execute("UPDATE media_items SET tmdb_id = 7")
        self._watch(conn, "amy", "f")
        conn.execute("UPDATE media_items SET title = 'New Name'")
        assert len(person_plays(conn, "amy")) == 1


class TestMaking:
    def test_neither_persons_titles_nor_any_name_reach_the_others_prompt(self, conn, openai):
        _plays(conn, "amy", "Alpha", "Beta", "Gamma", "Delta", "Epsilon")
        _plays(conn, "bob", "Rho", "Sigma", "Tau", "Phi", "Psi")
        insert_user_pick(conn, "bob", title="Bobs Earlier Pick", batch_id="2026-05-10")
        insert_suggestion(conn, title="Household Pick", category="personal", batch_id="2026-05-10")

        _make(conn, "amy")
        _make(conn, "bob")
        refresh_recommendations(conn, secret_key=_KEY)

        amys, bobs, shared = openai.prompts
        assert "Alpha" in amys and "Rho" in bobs
        assert "Bobs Earlier Pick" in bobs
        for title in ("Rho", "Sigma", "Tau", "Phi", "Psi", "Bobs Earlier Pick", "Household Pick"):
            assert title not in amys and (title not in shared)
        for title in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon"):
            assert title not in bobs and title not in shared
        for prompt in openai.prompts:
            assert "amy" not in prompt and "bob" not in prompt

    def test_picks_are_stored_as_the_persons_own_and_never_among_the_shared(self, conn, openai):
        _plays(conn, "amy", "Beta", "Gamma", "Delta", "Epsilon")
        _play(conn, "amy", "Alpha", _ago(hours=1))
        openai.items = [_pick("New One", because_of=["H1"])]

        assert _make(conn, "amy") == 1

        assert _titles(conn, "amy") == ["New One"]
        assert _titles(conn, "bob") == []
        assert conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0] == 0
        [row] = user_picks.fetch_picks(conn, "amy")
        pick = user_picks.fetch_pick(conn, "amy", row["id"])
        assert pick.because_of == ("Alpha",)
        assert user_picks.fetch_pick(conn, "bob", row["id"]) is None

    def test_a_failed_second_pass_still_stores_the_first(self, conn, openai, monkeypatch):
        _plays(conn, "amy", "Alpha", "Beta", "Gamma", "Delta", "Epsilon")
        answers = iter([[_pick(f"New {n}") for n in range(20)], []])
        monkeypatch.setattr(prompts, "call_openai", lambda *_a, **_k: next(answers))
        assert _make(conn, "amy") == 14
        assert next(answers, "both calls made") == "both calls made"
        assert conn.execute("SELECT status FROM user_pick_runs").fetchone()[0] == "done"

    def test_under_five_titles_nothing_is_sent(self, conn, openai):
        _plays(conn, "amy", "Alpha", "Beta", "Gamma", "Delta")
        openai.items = [_pick("New One")]

        assert _make(conn, "amy") == 0
        assert openai.prompts == []

    def test_what_the_person_knows_or_the_library_holds_is_left_out(self, conn, openai):
        _plays(conn, "amy", "Alpha", "Beta", "Gamma", "Delta", "Epsilon")
        _link(conn, "amy")
        _plex_play(conn, "amy", "Gone Film", _ago(days=2))
        insert_media_item(conn, id="held", title="Held (1999)", plex_rating_key="held")
        conn.execute(
            "INSERT INTO deleted_media (audit_id, media_type, title)"
            " VALUES (1, 'tv_season', 'Deleted Show')"
        )
        insert_user_pick(conn, "amy", title="Picked Before", batch_id="2026-05-01")
        insert_user_pick(conn, "amy", title="Picked Long Ago", batch_id="2026-03-01")
        insert_suggestion(conn, title="Shared Now", batch_id="2026-05-19")
        insert_suggestion(conn, title="Shared Before", batch_id="2026-05-12")
        openai.items = [
            _pick("Alpha"),
            _pick("Gone Film"),
            _pick("Held"),
            _pick("Deleted Show: Season 2", "tv"),
            _pick("Picked Before"),
            _pick("Shared Now"),
            _pick("Fresh"),
            _pick("Fresh"),
            _pick("Shared Before"),
            _pick("Picked Long Ago"),
        ]

        _make(conn, "amy")

        today = [r["title"] for r in user_picks.fetch_picks(conn, "amy", latest_only=True)]
        assert today == ["Fresh", "Shared Before", "Picked Long Ago"]

    def test_fourteen_are_kept(self, conn, openai):
        _plays(conn, "amy", "Alpha", "Beta", "Gamma", "Delta", "Epsilon")
        openai.items = [_pick(f"New {n}") for n in range(20)]
        assert _make(conn, "amy") == 14

    def test_a_second_run_today_keeps_a_downloaded_pick_and_replaces_the_rest(self, conn, openai):
        _plays(conn, "amy", "Alpha", "Beta", "Gamma", "Delta", "Epsilon")
        today = NOW.strftime("%Y-%m-%d")
        insert_user_pick(conn, "amy", title="Had", batch_id=today, downloaded_at=_ago(hours=1))
        insert_user_pick(conn, "amy", title="Unwanted", batch_id=today)
        insert_user_pick(conn, "bob", title="Bobs", batch_id=today)
        openai.items = [_pick("Had"), _pick("New")]

        _make(conn, "amy")

        marks = {r["title"]: r["downloaded_at"] for r in user_picks.fetch_picks(conn, "amy")}
        assert marks == {"Had": _ago(hours=1), "New": None}
        assert _titles(conn, "bob") == ["Bobs"]

    def test_a_run_that_gives_nothing_keeps_the_earlier_batch_and_is_marked_failed(
        self, conn, openai
    ):
        _plays(conn, "amy", "Alpha", "Beta", "Gamma", "Delta", "Epsilon")
        insert_user_pick(conn, "amy", title="Earlier", batch_id="2026-05-01")

        assert _make(conn, "amy") == 0

        assert _titles(conn, "amy") == ["Earlier"]
        assert conn.execute("SELECT status FROM user_pick_runs").fetchone()[0] == "failed"

    def test_only_the_servers_owner_has_the_servers_stars_sent(self, conn, openai):
        plex = MagicMock()
        plex.get_user_ratings.return_value = [{"title": "Starred", "type": "movie", "stars": 5.0}]
        for name, account in (("amy", 1), ("bob", 8)):
            _plays(conn, name, f"{name[0]}1x", f"{name[0]}2x", f"{name[0]}3x", "Four", "Five")
            _link(conn, name, account)

        _make(conn, "amy", plex)
        _make(conn, "bob", plex)

        assert ["Starred" in p for p in openai.prompts] == [True, False]

    def test_stars_plex_will_not_give_do_not_cost_the_picks(self, conn, openai):
        plex = MagicMock()
        plex.get_user_ratings.side_effect = requests.ConnectionError("down")
        _plays(conn, "amy", "Alpha", "Beta", "Gamma", "Delta", "Epsilon")
        _link(conn, "amy", 1)
        openai.items = [_pick("New")]
        assert _make(conn, "amy", plex) == 1

    def test_picks_made_from_plex_plays_are_marked_so_and_dropped_if_unlinked_meanwhile(
        self, conn, openai, monkeypatch
    ):
        _plays(conn, "amy", "Alpha", "Beta", "Gamma", "Delta")
        _link(conn, "amy")
        _plex_play(conn, "amy", "Epsilon", _ago(days=1))
        openai.items = [_pick("New")]

        assert _make(conn, "amy") == 1
        assert conn.execute("SELECT uses_plex FROM user_picks").fetchone()[0] == 1

        conn.execute("DELETE FROM user_picks")
        conn.commit()
        real = personal.enrich_recommendations

        def unlink_then_enrich(*args, **kwargs):
            conn.execute("DELETE FROM plex_links")
            conn.commit()
            return real(*args, **kwargs)

        monkeypatch.setattr(personal, "enrich_recommendations", unlink_then_enrich)
        assert _make(conn, "amy") == 0
        assert _titles(conn, "amy") == []

    def test_picks_from_a_link_replaced_while_they_were_made_are_dropped(
        self, conn, openai, monkeypatch
    ):
        _plays(conn, "amy", "Alpha", "Beta", "Gamma", "Delta")
        _link(conn, "amy")
        _plex_play(conn, "amy", "Epsilon", _ago(days=1))
        openai.items = [_pick("New")]
        real = personal.enrich_recommendations

        def relink_then_enrich(*args, **kwargs):
            conn.execute("DELETE FROM plex_links")
            _link(conn, "amy", 9)
            conn.execute("UPDATE plex_links SET linked_at = ?", (_ago(hours=0),))
            conn.commit()
            return real(*args, **kwargs)

        monkeypatch.setattr(personal, "enrich_recommendations", relink_then_enrich)
        assert _make(conn, "amy") == 0
        assert _titles(conn, "amy") == []

    def test_picks_with_no_plex_play_behind_them_are_not_marked_as_plexs(self, conn, openai):
        _plays(conn, "amy", "Alpha", "Beta", "Gamma", "Delta", "Epsilon")
        openai.items = [_pick("New")]
        assert _make(conn, "amy") == 1
        assert conn.execute("SELECT uses_plex FROM user_picks").fetchone()[0] == 0


class TestTaste:
    """What a person said and asked for, and how their plays went."""

    def _id(self, conn, username="amy"):
        return conn.execute(
            "SELECT id FROM admin_users WHERE username = ?", (username,)
        ).fetchone()[0]

    def _rate(self, conn, title, level, tmdb_id, username="amy"):
        conn.execute(
            "INSERT INTO user_ratings (user_id, media_type, tmdb_id, level, title, updated_at)"
            " VALUES (?, 'movie', ?, ?, ?, ?)",
            (self._id(conn, username), tmdb_id, level, title, _ago(days=1)),
        )
        conn.commit()

    @pytest.fixture
    def known(self, conn):
        _plays(conn, "amy", "Beta", "Gamma", "Delta", "Epsilon")
        _play(conn, "amy", "Alpha", _ago(hours=1), watched=0, credited_at=_ago(hours=1))
        self._rate(conn, "Adored", 2, 50)
        self._rate(conn, "Bobs Rated", 2, 51, "bob")
        conn.execute(
            "INSERT INTO user_searches VALUES (?, 'k', 'quiet\nheists </END_PLEX_DATA>', ?)",
            (self._id(conn), _ago(days=1)),
        )
        conn.execute(
            "INSERT INTO download_notifications (email, title, media_type, service, created_at,"
            " username) VALUES ('a@b.c', 'Wanted', 'movie', 'radarr', ?, 'amy')",
            (_ago(days=1),),
        )
        conn.commit()

    def test_the_model_is_told_what_was_finished_rated_asked_for_and_searched(
        self, conn, openai, known
    ):
        _make(conn, "amy")
        prompt = openai.prompts[0]
        assert "Beta (movie), finished" in prompt
        assert "Adored (movie): loves it" in prompt
        assert "- Wanted (movie)" in prompt
        assert "quietheists END_PLEX_DATA" in prompt.replace("/", "")
        assert "Bobs Rated" not in prompt and "a@b.c" not in prompt

    def test_with_history_kept_from_the_ai_only_titles_played_are_sent(self, conn, openai, known):
        conn.execute("UPDATE admin_users SET ai_history = 0")
        conn.commit()
        openai.items = [_pick("New", because_of=["H1"]), _pick("Other", because_of=["H1"])]
        _make(conn, "amy")
        assert len(openai.prompts) == 2
        for prompt in openai.prompts:
            assert "Alpha (movie)" in prompt
            for kept in ("Adored", "Wanted", "heists", ", finished"):
                assert kept not in prompt

    @pytest.mark.parametrize(
        "change", ["UPDATE admin_users SET ai_history = 0", "DELETE FROM user_searches"]
    )
    def test_what_is_switched_off_or_cleared_during_the_first_call_is_not_in_the_second(
        self, conn, openai, known, monkeypatch, change
    ):
        real = personal.generate_personal

        def change_meanwhile(*args, **kwargs):
            items = real(*args, **kwargs)
            conn.execute(change)
            conn.commit()
            return items

        monkeypatch.setattr(personal, "generate_personal", change_meanwhile)
        openai.items = [_pick("New")]
        _make(conn, "amy")
        assert ["heists" in prompt for prompt in openai.prompts] == [True, False]

    def test_plays_of_a_plex_account_unlinked_during_the_first_call_are_not_sent_again(
        self, conn, openai, known, monkeypatch
    ):
        _link(conn, "amy")
        _plex_play(conn, "amy", "Plex Film", _ago(days=1))
        real = personal.generate_personal

        def unlink_meanwhile(*args, **kwargs):
            items = real(*args, **kwargs)
            conn.execute("DELETE FROM plex_links")
            conn.commit()
            return items

        monkeypatch.setattr(personal, "generate_personal", unlink_meanwhile)
        openai.items = [_pick("New")]
        _make(conn, "amy")
        assert ["Plex Film" in prompt for prompt in openai.prompts] == [True]

    @pytest.mark.parametrize("ai_history", [0, 1])
    def test_a_strong_no_is_never_picked_and_a_not_for_me_comes_last(
        self, conn, openai, monkeypatch, ai_history
    ):
        conn.execute("UPDATE admin_users SET ai_history = ?", (ai_history,))
        _plays(conn, "amy", "Alpha", "Beta", "Gamma", "Delta", "Epsilon")
        self._rate(conn, "Hated", -2, 61)
        self._rate(conn, "Meh", -1, 62)
        self._rate(conn, "Known", 1, 63)
        ids = {"Hated": 61, "Meh": 62, "Known": 63}
        client = MagicMock()
        client.search_results_or_raise.side_effect = lambda title, **_k: [
            {"id": ids.get(title, 99), "title": title}
        ]
        client.details.return_value = None
        client.shape_card = TmdbClient.shape_card
        monkeypatch.setattr(TmdbClient, "from_db", lambda *_a: client)
        openai.items = [_pick(t) for t in ("Meh", "Hated", "Known", "Fine")]
        _make(conn, "amy")
        assert _titles(conn, "amy") == ["Fine", "Meh"]

    def test_a_title_rated_while_the_model_thinks_is_left_out(self, conn, openai, monkeypatch):
        _plays(conn, "amy", "Alpha", "Beta", "Gamma", "Delta", "Epsilon")
        real = personal.generate_personal

        def rate_meanwhile(*args, **kwargs):
            items = real(*args, **kwargs)
            self._rate(conn, "Hated", -2, 61)
            return items

        monkeypatch.setattr(personal, "generate_personal", rate_meanwhile)
        openai.items = [_pick("Hated"), _pick("Fine")]
        _make(conn, "amy")
        assert _titles(conn, "amy") == ["Fine"]

    def test_a_played_title_rated_down_leads_to_no_pick(self, conn, openai):
        _plays(conn, "amy", "Beta", "Gamma", "Delta", "Epsilon")
        _play(conn, "amy", "Alpha", _ago(hours=1))
        conn.execute("UPDATE media_items SET tmdb_id = 5 WHERE title = 'Alpha'")
        self._rate(conn, "Another Name", -1, 5)
        openai.items = [_pick("New", because_of=["H1"])]
        _make(conn, "amy")
        [row] = user_picks.fetch_picks(conn, "amy")
        assert user_picks.fetch_pick(conn, "amy", row["id"]).because_of == ()

    @pytest.mark.parametrize(
        ("row", "days", "note"),
        [
            ({}, 1, "finished"),
            ({"seen_at": _ago(days=30)}, 1, "watched again"),
            (
                {"watched": 0, "credited_at": "x", "position_s": 30, "duration_s": 90},
                60,
                "left part way",
            ),
            ({"watched": 0, "credited_at": "x", "position_s": 30, "duration_s": 90}, 3, ""),
            ({"show": "s"}, 60, "left after 1 episode"),
            ({"show": "s"}, 3, "1 episode"),
        ],
    )
    def test_how_a_play_went(self, conn, row, days, note):
        _play(conn, "amy", "Film", _ago(days=days), **row)
        assert [p.note for p in person_plays(conn, "amy")] == [note]

    def test_a_film_played_in_plex_twice_weeks_apart_was_watched_again(self, conn):
        _link(conn, "amy")
        for days in (40, 3):
            _plex_play(conn, "amy", "Film", _ago(days=days))
        assert [p.note for p in person_plays(conn, "amy")] == ["watched again"]


class TestRunsItBelongsTo:
    def test_a_person_made_anew_meanwhile_gets_none_of_the_old_ones_picks(
        self, conn, openai, monkeypatch
    ):
        _plays(conn, "amy", "Alpha", "Beta", "Gamma", "Delta", "Epsilon")
        openai.items = [_pick("New")]
        real = personal.enrich_recommendations

        def replace_then_enrich(*args, **kwargs):
            conn.execute("DELETE FROM admin_users WHERE username = 'amy'")
            _person(conn, "amy")
            assert isinstance(user_picks.start_run(conn, "amy", "person", NOW), int)
            return real(*args, **kwargs)

        monkeypatch.setattr(personal, "enrich_recommendations", replace_then_enrich)

        assert _make(conn, "amy") == 0
        assert _titles(conn, "amy") == []
        assert conn.execute("SELECT finished_at FROM user_pick_runs").fetchone()[0] is None

    def test_a_run_already_ended_stores_nothing(self, conn, openai):
        _plays(conn, "amy", "Alpha", "Beta", "Gamma", "Delta", "Epsilon")
        openai.items = [_pick("New")]
        run = user_picks.start_run(conn, "amy", "scan", NOW)
        user_picks.finish_run(conn, run, "failed", NOW)
        assert personal.make_picks(conn, "amy", None, run, secret_key=_KEY) == 0

    def test_looking_up_gets_only_what_the_model_left_of_a_persons_time(
        self, conn, openai, monkeypatch
    ):
        _plays(conn, "amy", "Alpha", "Beta", "Gamma", "Delta", "Epsilon")
        openai.items = [_pick("New")]
        given = []
        monkeypatch.setattr(
            personal, "enrich_recommendations", lambda *_a, **k: given.append(k["seconds"])
        )
        for took in (10.0, 125.0):
            times = iter([0.0, took])
            run = user_picks.start_run(conn, "amy", "scan", NOW)
            personal.make_picks(
                conn, "amy", None, run, secret_key=_KEY, clock=lambda t=times: next(t)
            )
        assert given == [60.0, 55.0]


class TestEligible:
    def _done(self, conn, username, when, status="done"):
        conn.execute(
            "INSERT INTO user_pick_runs (username, requested_by, status, started_at, finished_at)"
            " VALUES (?, 'scan', ?, ?, ?)",
            (username, status, when, when),
        )
        conn.commit()

    def test_five_titles_make_a_person_eligible(self, conn):
        _plays(conn, "amy", "A", "B", "C", "D", "E")
        _plays(conn, "bob", "A", "B", "C", "D")
        assert personal.eligible_people(conn, NOW) == ["amy"]

    def test_nothing_played_since_the_last_batch_waits_until_it_is_28_days_old(self, conn):
        _plays(conn, "amy", "A", "B", "C", "D", "E", when=_ago(days=29))
        _plays(conn, "bob", "A", "B", "C", "D", "E", when=_ago(days=29))
        self._done(conn, "amy", _ago(days=27))
        self._done(conn, "bob", _ago(days=28, hours=1))
        for name in ("amy", "bob"):
            insert_user_pick(conn, name, batch_id="2026-04-22")
        assert personal.eligible_people(conn, NOW) == ["bob"]

    def test_a_person_left_without_picks_is_due_at_once(self, conn):
        for name in ("amy", "bob"):
            _plays(conn, name, "A", "B", "C", "D", "E", when=_ago(days=9))
            self._done(conn, name, _ago(days=2))
        insert_user_pick(conn, "bob", batch_id="2026-05-18")
        assert personal.eligible_people(conn, NOW) == ["amy"]

    def test_a_play_since_the_last_batch_makes_a_new_one_due(self, conn):
        _plays(conn, "amy", "A", "B", "C", "D", "E", when=_ago(days=1))
        self._done(conn, "amy", _ago(days=2))
        assert personal.eligible_people(conn, NOW) == ["amy"]

    def test_a_person_away_for_30_days_has_none_made(self, conn):
        _plays(conn, "amy", "A", "B", "C", "D", "E", when=_ago(days=40))
        _plays(conn, "bob", "A", "B", "C", "D", "E", when=_ago(days=40))
        conn.execute(
            "INSERT INTO admin_sessions (token, username, created_at, expires_at, last_used_at)"
            " VALUES ('t', 'bob', ?, ?, ?)",
            (_ago(days=40), _ago(days=-1), _ago(days=3)),
        )
        assert personal.eligible_people(conn, NOW) == ["bob"]

    def test_a_scan_leaves_out_a_person_whose_last_run_failed_under_a_day_ago(self, conn):
        for name in ("amy", "bob"):
            _plays(conn, name, "A", "B", "C", "D", "E")
        self._done(conn, "amy", _ago(hours=23), "failed")
        self._done(conn, "bob", _ago(hours=25), "failed")
        assert personal.eligible_people(conn, NOW) == ["bob"]
        self._done(conn, "amy", _ago(hours=22))
        conn.execute("UPDATE watch_progress SET updated_at = ?", (_ago(hours=1),))
        assert personal.eligible_people(conn, NOW) == ["bob", "amy"]

    def test_the_one_longest_without_a_try_comes_first(self, conn):
        _plays(conn, "amy", "A", "B", "C", "D", "E")
        _plays(conn, "bob", "A", "B", "C", "D", "E")
        self._done(conn, "amy", _ago(days=3))
        self._done(conn, "bob", _ago(days=9))
        assert personal.eligible_people(conn, NOW) == ["bob", "amy"]


class TestScan:
    @pytest.fixture
    def crowd(self, conn):
        for n in range(12):
            _person(conn, f"u{n:02}")
            _plays(conn, f"u{n:02}", "A", "B", "C", "D", "E")

    def test_at_most_ten_people_are_served(self, conn, crowd, openai):
        openai.items = [_pick("New")]
        assert personal.refresh_people(conn, None, secret_key=_KEY, clock=lambda: 0.0) == 10
        served = conn.execute("SELECT COUNT(DISTINCT username) FROM user_picks").fetchone()[0]
        assert served == 10
        # No person costs more than two calls: the proposals and one second pass.
        assert len(openai.prompts) == 20

    def test_none_is_begun_after_five_minutes(self, conn, crowd, openai):
        openai.items = [_pick("New")]
        times = iter([0.0, 0.0, 200.0, 301.0])
        made = personal.refresh_people(conn, None, secret_key=_KEY, clock=lambda: next(times))
        assert made == 2

    def test_one_persons_failure_leaves_the_others_theirs(self, conn, openai, monkeypatch):
        _plays(conn, "amy", "A", "B", "C", "D", "E")
        _plays(conn, "bob", "A", "B", "C", "D", "E")
        openai.items = [_pick("New")]
        real = personal.person_plays
        calls = []

        def fail_for_amy(c, username):
            calls.append(username)
            # The first reading decides who is due, the second makes the picks.
            if username == "amy" and calls.count("amy") == 2:
                raise RuntimeError("boom")
            return real(c, username)

        monkeypatch.setattr(personal, "person_plays", fail_for_amy)

        assert personal.refresh_people(conn, None, secret_key=_KEY) == 1

        assert (_titles(conn, "amy"), _titles(conn, "bob")) == ([], ["New"])
        statuses = conn.execute("SELECT username, status FROM user_pick_runs ORDER BY username")
        assert [tuple(r) for r in statuses] == [("amy", "failed"), ("bob", "done")]

    def test_without_an_openai_key_no_run_is_recorded(self, conn, openai):
        conn.execute("DELETE FROM settings WHERE key = 'openai_api_key'")
        _plays(conn, "amy", "A", "B", "C", "D", "E")
        assert personal.refresh_people(conn, None, secret_key=_KEY) == 0
        assert conn.execute("SELECT COUNT(*) FROM user_pick_runs").fetchone()[0] == 0


class TestRefreshLimits:
    def _run(self, conn, username, status, when, by="person"):
        conn.execute(
            "INSERT INTO user_pick_runs (username, requested_by, status, started_at, finished_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (username, by, status, when, None if status == "running" else when),
        )
        conn.commit()

    def test_a_refresh_that_made_picks_is_followed_by_a_week_of_waiting(self, conn):
        self._run(conn, "amy", "done", _ago(days=6, hours=23))
        assert user_picks.start_run(conn, "amy", "person", NOW) == RefreshState(
            "wait", (NOW + timedelta(hours=1)).isoformat()
        )
        assert isinstance(user_picks.start_run(conn, "bob", "person", NOW), int)

    @pytest.mark.parametrize(
        ("status", "by", "when"),
        [
            ("failed", "person", {"hours": 1}),
            ("done", "scan", {"hours": 1}),
            ("done", "person", {"days": 7}),
        ],
    )
    def test_a_failed_or_scan_run_or_one_a_week_old_holds_no_one_back(self, conn, status, by, when):
        self._run(conn, "amy", status, _ago(**when), by)
        assert isinstance(user_picks.start_run(conn, "amy", "person", NOW), int)

    def test_ten_refreshes_a_day_are_all_there_are_for_everyone(self, conn):
        for hours in range(10):
            self._run(conn, "bob", "failed", _ago(hours=hours + 1))
        assert user_picks.start_run(conn, "amy", "person", NOW) == RefreshState(
            "daily_limit", (NOW + timedelta(hours=14)).isoformat()
        )

    def test_one_persons_failures_cannot_use_up_everyones_day(self, conn):
        for hours in (1, 2, 3):
            self._run(conn, "amy", "failed", _ago(hours=hours))
        assert user_picks.start_run(conn, "amy", "person", NOW) == RefreshState(
            "daily_limit", (NOW + timedelta(hours=21)).isoformat()
        )
        assert isinstance(user_picks.start_run(conn, "bob", "person", NOW), int)

    def test_a_second_run_waits_for_the_first_but_not_for_a_dead_one(self, conn):
        self._run(conn, "amy", "running", _ago(minutes=29))
        assert user_picks.start_run(conn, "amy", "person", NOW) == RefreshState("running")
        assert user_picks.start_run(conn, "amy", "scan", NOW) == RefreshState("running")
        conn.execute("UPDATE user_pick_runs SET started_at = ?", (_ago(minutes=31),))
        conn.commit()
        assert isinstance(user_picks.start_run(conn, "amy", "person", NOW), int)


class TestSharedPicks:
    def test_every_run_drops_stored_picks_found_to_be_adult_and_marks_its_own_as_checked(
        self, conn, openai, monkeypatch
    ):
        insert_suggestion(conn, title="Adult", tmdb_id=6, batch_id="2026-05-01")
        conn.commit()
        client = MagicMock()
        client.search_results_or_raise.return_value = [{"id": 5, "title": "New"}]
        client.details_or_raise.side_effect = lambda _t, n: {"id": n, "adult": n == 6}
        client.details.return_value = {"id": 5, "adult": False}
        client.shape_card = TmdbClient.shape_card
        client.shape_detail = TmdbClient.shape_detail
        monkeypatch.setattr(TmdbClient, "from_db", lambda *_a: client)
        openai.items = [_pick("New")]

        refresh_recommendations(conn, secret_key=_KEY)

        rows = conn.execute("SELECT title, adult_checked FROM suggestions").fetchall()
        assert [tuple(r) for r in rows] == [("New", 1)]
        assert "adult" in openai.prompts[0]

    def test_the_shared_batch_leaves_out_what_the_library_holds_or_it_had_lately(
        self, conn, openai
    ):
        insert_media_item(conn, id="held", title="Held", plex_rating_key="held")
        insert_suggestion(conn, title="Lately", batch_id="2026-05-01")
        insert_suggestion(conn, title="Household", category="personal", batch_id="2026-05-01")
        openai.items = [_pick("Held"), _pick("Lately"), _pick("Household"), _pick("New")]

        refresh_recommendations(conn, secret_key=_KEY)

        rows = conn.execute("SELECT title FROM suggestions WHERE batch_id = '2026-05-20'")
        assert [r["title"] for r in rows] == ["Household", "New"]

    def test_the_prompt_lists_the_newest_earlier_titles_first(self, conn, openai):
        insert_suggestion(conn, title="Older", batch_id="2026-05-01")
        insert_suggestion(conn, title="Newer", batch_id="2026-05-10")
        refresh_recommendations(conn, secret_key=_KEY)
        assert '["Newer", "Older"]' in openai.prompts[0]

    def test_a_pick_downloaded_today_may_be_picked_again(self, conn, openai):
        insert_suggestion(conn, title="Had", batch_id="2026-05-20", downloaded_at=_ago(hours=1))
        openai.items = [_pick("Had")]
        assert refresh_recommendations(conn, secret_key=_KEY) == 1
        rows = conn.execute("SELECT title, downloaded_at FROM suggestions").fetchall()
        assert [tuple(r) for r in rows] == [("Had", _ago(hours=1))]
