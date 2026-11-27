"""Search asks the AI on its own as a person types, unless they switched it off: the panel
says how many answers are left today and whether to ask on its own; a question may name the
title the person wants more like; an answer has a short heading."""

# ruff: noqa: F811

from __future__ import annotations

import pytest

from mediaman.core.time import now_utc
from tests.helpers.factories import insert_settings
from tests.unit.web.test_search_assist import (  # noqa: F401  (fixtures)
    BASE,
    Q,
    _ask,
    _id,
    _title,
    _world,
    amy,
    model,
    people,
    tmdb,
)

ALIBI = {"tmdb_id": 2, "kind": "movie", "title": "Alibi"}


def _spent(conn, name, count, kind="ask"):
    conn.executemany(
        "INSERT INTO user_ai_runs (user_id, kind, status, started_at, finished_at)"
        " VALUES (?, ?, 'failed', ?, '2026')",
        [(_id(conn, name), kind, now_utc().isoformat())] * count,
    )
    conn.commit()


class TestLeft:
    def test_an_answer_asked_on_its_own_counts_and_one_kept_is_free(self, amy, model):
        model.items = [_title("Alibi")]
        assert amy.get(BASE).json()["left"] == 20

        _ask(amy, auto=True)
        _ask(amy, auto=True)

        assert (amy.get(BASE).json()["left"], len(model.prompts)) == (19, 1)

    def test_what_is_left_is_the_smaller_of_the_persons_day_and_everyones(self, people, conn):
        insert_settings(conn, ai_questions_per_day="5", ai_everyone_per_day="8")
        _spent(conn, "amy-secret", 2)
        assert people["amy"].get(BASE).json()["left"] == 3

        _spent(conn, "bob-secret", 5, "phrases")
        assert people["amy"].get(BASE).json()["left"] == 1
        _spent(conn, "bob-secret", 3, "phrases")
        assert people["amy"].get(BASE).json()["left"] == 0

    def test_a_question_asked_on_its_own_is_not_kept_as_a_search(self, amy, model):
        model.items = [_title("Alibi")]
        _ask(amy, auto=True)
        assert amy.get(BASE).json()["recent"] == []


class TestSwitch:
    def test_asking_on_its_own_is_on_until_the_person_switches_it_off(self, people):
        assert people["amy"].get(BASE).json()["auto"] is True

        resp = people["amy"].put(f"{BASE}/history", json={"auto": False})

        assert (resp.status_code, resp.json()) == (
            200,
            {"ok": True, "enabled": True, "auto": False},
        )
        assert people["amy"].get(BASE).json()["auto"] is False
        assert people["amy"].get(f"{BASE}/history").json()["auto"] is False
        assert people["bob"].get(BASE).json()["auto"] is True

    def test_switched_off_nothing_is_sent_on_its_own_but_a_pressed_question_is(self, amy, model):
        amy.put(f"{BASE}/history", json={"auto": False})

        resp = amy.post(f"{BASE}/ask", json={"q": Q, "auto": True})
        assert (resp.status_code, resp.json()["error"], model.prompts) == (409, "auto_off", [])
        assert amy.post(f"{BASE}/ask", json={"q": Q}).status_code == 202

    @pytest.mark.parametrize("body", [{"auto": "no"}, {}, {"auto": None}])
    def test_the_switch_takes_only_true_or_false(self, amy, body):
        assert amy.put(f"{BASE}/history", json=body).status_code == 422
        assert amy.get(BASE).json()["auto"] is True

    def test_switching_history_leaves_asking_on_its_own_as_it_was(self, amy):
        amy.put(f"{BASE}/history", json={"auto": False})
        assert amy.put(f"{BASE}/history", json={"enabled": False}).json() == {
            "ok": True,
            "enabled": False,
            "auto": False,
        }


class TestQuestions:
    def test_a_question_of_two_words_is_asked(self, amy, model):
        assert amy.post(f"{BASE}/ask", json={"q": "netflix originals"}).status_code == 202
        assert amy.get(BASE).json()["min_words"] == 2

    def test_an_answer_says_its_heading(self, amy, model):
        model.items = [_title("Alibi"), {"kind": "heading", "text": "Heists that go wrong"}]
        assert _ask(amy)["heading"] == "Heists that go wrong"

    def test_an_answer_kept_from_before_headings_is_still_shown(self, amy, model, conn):
        model.items = [_title("Alibi")]
        _ask(amy)
        conn.execute("UPDATE user_ai_answers SET answer = json_remove(answer, '$.heading')")
        conn.commit()

        found = _ask(amy)

        assert (found["status"], found["heading"], len(model.prompts)) == ("done", None, 1)
        assert [t["title"] for t in found["others"]] == ["Alibi"]


class TestNamedTitle:
    def test_the_title_named_is_sent_as_tmdb_knows_it_and_left_out(self, amy, model):
        model.items = [_title("Alibi"), _title("Kestrel")]
        found = _ask(amy, q="something similar to alibi", like=ALIBI)

        assert [t["title"] for t in found["owned"] + found["others"]] == ["Kestrel"]
        assert '"title": ["Alibi"], "year": 2019' in model.prompts[-1]

    def test_the_same_words_naming_another_title_are_asked_afresh(self, amy, model):
        model.items = [_title("Kestrel")]
        _ask(amy, q="more like this one", like=ALIBI)
        _ask(amy, q="more like this one", like={**ALIBI, "tmdb_id": 5, "title": "Harbour"})
        assert len(model.prompts) == 2 and "Harbour" in model.prompts[-1]

    def test_a_title_tmdb_does_not_have_is_not_found(self, amy, model):
        resp = amy.post(f"{BASE}/ask", json={"q": Q, "like": {**ALIBI, "tmdb_id": 99}})
        assert (resp.status_code, model.prompts) == (404, [])

    def test_with_tmdb_out_of_reach_the_title_as_the_page_named_it_is_sent(self, amy, model, tmdb):
        tmdb.failing = True
        amy.post(f"{BASE}/ask", json={"q": Q, "like": {**ALIBI, "title": "Alibi (2019)"}})
        assert '"title": ["Alibi (2019)"]' in model.prompts[-1]

    @pytest.mark.parametrize(
        "like",
        [
            {**ALIBI, "kind": "person"},
            {**ALIBI, "tmdb_id": 0},
            {**ALIBI, "title": ""},
            {**ALIBI, "title": "x" * 201},
            {"tmdb_id": 2, "kind": "movie"},
        ],
    )
    def test_a_named_title_that_is_not_one_is_refused(self, amy, model, like):
        assert amy.post(f"{BASE}/ask", json={"q": Q, "like": like}).status_code == 422
        assert model.prompts == []
