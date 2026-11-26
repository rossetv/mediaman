"""A question asked again is answered from what was kept unless its answer would be another
now: the next day for a question of new things, after a new play or rating for a question
about the asker. A run shows the titles it has checked while it checks the rest, and says
which group holds the best fit."""

# ruff: noqa: F811

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from mediaman.core.time import now_utc
from mediaman.web.repository.watch_progress import save_progress
from mediaman.web.routes.search import assist
from tests.helpers.factories import insert_media_item
from tests.unit.web.test_search_assist import (  # noqa: F401  (fixtures)
    BASE,
    Q,
    _ask,
    _own,
    _play,
    _rate,
    _title,
    _world,
    amy,
    model,
    people,
    tmdb,
)

PERSONAL = "heist films I would like"

# Taken before any test puts a thread that runs at once in its place.
_THREAD = threading.Thread


def test_a_question_asked_again_in_other_case_spacing_and_signs_is_not_sent_again(amy, model):
    model.items = [_title("Alibi")]
    _ask(amy)
    again = _ask(amy, q="A heist, that  goes WRONG!")
    assert ([t["title"] for t in again["others"]], len(model.prompts)) == (["Alibi"], 1)


@pytest.mark.parametrize(("q", "asked_again"), [("new heist films out now", True), (Q, False)])
def test_a_question_of_new_things_is_asked_again_the_next_day(
    amy, model, monkeypatch, q, asked_again
):
    model.items = [_title("Alibi")]
    _ask(amy, q=q)
    _ask(amy, q=q)
    assert len(model.prompts) == 1

    tomorrow = now_utc() + timedelta(days=1)
    monkeypatch.setattr(assist, "now_utc", lambda: tomorrow)
    _ask(amy, q=q)
    assert len(model.prompts) == (2 if asked_again else 1)


def _saved_position(conn):
    conn.execute(
        "UPDATE watch_progress SET position_s = 3, updated_at = '2027' WHERE rating_key = '101'"
    )
    conn.commit()


def _counted_play(conn):
    _own(conn, 5)
    _play(conn, "105")
    conn.execute("UPDATE watch_progress SET credited_at = '2027' WHERE rating_key = '105'")
    conn.commit()


@pytest.mark.parametrize(
    ("change", "asked_again"),
    [
        (_saved_position, False),
        (lambda conn: _rate(conn, 4, 2, "Kestrel"), True),
        (_counted_play, True),
    ],
)
def test_a_question_about_the_asker_is_asked_again_once_their_taste_changes(
    amy, model, conn, change, asked_again
):
    _own(conn, 1)
    _play(conn, "101")
    model.items = [_title("Alibi")]
    _ask(amy, q=PERSONAL)
    _ask(amy, q=PERSONAL)
    assert len(model.prompts) == 1

    change(conn)
    _ask(amy, q=PERSONAL)
    assert len(model.prompts) == (2 if asked_again else 1)


NOON = datetime(2026, 11, 20, 12, tzinfo=UTC)


def _playing(conn, key, played_s, at):
    """A position saved by the player after *played_s* seconds played on the page."""
    save_progress(
        conn,
        username="amy-secret",
        rating_key=key,
        media_item_id=key,
        show_rating_key=None,
        position_s=played_s,
        duration_s=7200,
        played_s=played_s,
        now=at,
    )


def test_a_question_about_the_asker_is_found_again_while_they_play_and_its_chips_narrow(
    amy, model, conn
):
    _own(conn, 1)
    _playing(conn, "101", 400, NOON)
    model.items = [_title("Alibi"), {"kind": "chip", "text": "Funnier"}]
    _ask(amy, q=PERSONAL)

    _playing(conn, "101", 460, NOON + timedelta(minutes=1))
    again = _ask(amy, q=PERSONAL)
    assert ([t["title"] for t in again["others"]], len(model.prompts)) == (["Alibi"], 1)

    narrowed = amy.post(f"{BASE}/ask", json={"q": PERSONAL, "refine": "Funnier"})
    assert (narrowed.status_code, len(model.prompts)) == (202, 2)


@pytest.mark.parametrize(
    ("change", "asked_again"),
    [
        (lambda conn: _rate(conn, 4, 2, "Kestrel"), True),
        (lambda conn: _playing(conn, "105", 400, NOON + timedelta(hours=1)), True),
        (lambda conn: _playing(conn, "105", 60, NOON + timedelta(hours=1)), False),
    ],
)
def test_a_question_about_the_asker_is_asked_again_after_a_rating_or_a_new_play_that_counts(
    amy, model, conn, change, asked_again
):
    _own(conn, 1, 5)
    _playing(conn, "101", 400, NOON)
    model.items = [_title("Alibi")]
    _ask(amy, q=PERSONAL)

    change(conn)
    _ask(amy, q=PERSONAL)
    assert len(model.prompts) == (2 if asked_again else 1)


def test_a_question_about_the_asker_leaves_out_a_play_tmdb_does_not_know(amy, model, conn):
    insert_media_item(conn, id="102", title="Alibi", media_type="movie", plex_library_id=1)
    _play(conn, "102")
    model.items = [_title("Alibi"), _title("Orchard")]

    found = _ask(amy, q=PERSONAL)
    assert [t["title"] for t in found["owned"] + found["others"]] == ["Orchard"]


def test_a_card_says_which_of_their_titles_its_reason_follows_from(amy, model, conn):
    _rate(conn, 4, 2, "Kestrel")
    reason = "Because you liked Kestrel: another tight heist."
    model.items = [_title("Alibi", reason=reason), _title("Orchard")]

    cards = _ask(amy, q=PERSONAL)["others"]
    assert [(c["reason"], c["reason_lead"]) for c in cards] == [
        (reason, "Because you liked Kestrel"),
        ("It fits well", None),
    ]


def test_a_question_about_the_asker_leaves_out_what_they_played(amy, model, conn):
    _own(conn, 2)
    _play(conn, "102")
    model.items = [_title("Alibi"), _title("Orchard")]

    found = _ask(amy, q=PERSONAL)
    assert [t["title"] for t in found["owned"] + found["others"]] == ["Orchard"]
    found = _ask(amy)
    assert [t["title"] for t in found["owned"] + found["others"]] == ["Alibi", "Orchard"]


def test_an_answer_about_one_person_is_never_anothers(people, model, conn):
    # Plays of the same shape give both the same taste stamp: only whose it is tells them apart.
    _own(conn, 1, 6)
    _play(conn, "101")
    _play(conn, "106", user="bob-secret")
    model.items = [_title("Alibi")]
    _ask(people["amy"], q=PERSONAL)
    _ask(people["bob"], q=PERSONAL)

    assert len(model.prompts) == 2
    assert "Nine Locks" in model.prompts[0] and "Nine Locks" not in model.prompts[1]
    assert "Orchard" in model.prompts[1] and "Orchard" not in model.prompts[0]


@pytest.mark.parametrize(
    ("names", "lead"), [(["Train Job", "Alibi"], "others"), (["Alibi", "Train Job"], "owned")]
)
def test_the_group_of_the_best_fit_is_said_to_lead(amy, model, conn, names, lead):
    _own(conn, 2)
    model.items = [_title(n) for n in names]
    assert _ask(amy)["lead"] == lead


def test_a_run_shows_the_titles_checked_while_it_checks_the_rest(amy, model, tmdb, monkeypatch):
    monkeypatch.setattr(assist.threading, "Thread", _THREAD)
    at, go = threading.Event(), threading.Event()
    found = tmdb.search_results_or_raise

    def search(title, **kw):
        if title == "Orchard":
            at.set()
            go.wait(10)
        return found(title, **kw)

    monkeypatch.setattr(tmdb, "search_results_or_raise", search)
    model.items = [_title("Alibi"), _title("Orchard"), {"kind": "chip", "text": "Funnier"}]
    run = amy.post(f"{BASE}/ask", json={"q": Q}).json()["run_id"]
    try:
        assert at.wait(10)
        polled = amy.get(f"{BASE}/runs/{run}").json()
    finally:
        go.set()
        for _ in range(200):
            if amy.get(f"{BASE}/runs/{run}").json().get("status") != "running":
                break
            time.sleep(0.01)

    assert (polled["status"], polled["stage"], polled["lead"]) == ("running", "checking", "others")
    assert [t["title"] for t in polled["others"]] == ["Alibi"] and polled["owned"] == []
    assert "chips" not in polled and "instead" not in polled
