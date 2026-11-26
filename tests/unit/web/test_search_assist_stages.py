"""A question being answered says which step it is at, and an AI not offered says why to an
admin, who can change it, and to nobody else."""

# ruff: noqa: F811

from __future__ import annotations

import threading
import time

import pytest

from mediaman.services.openai import client as openai_client
from mediaman.services.openai.search_assist import answers
from mediaman.web.routes.search import assist
from tests.unit.web.test_search_assist import (  # noqa: F401  (fixtures)
    BASE,
    Q,
    _title,
    _world,
    amy,
    model,
    people,
    tmdb,
)

# Taken before any test puts a thread that runs at once in its place.
_THREAD = threading.Thread


def _admin(conn, name="amy-secret"):
    conn.execute("UPDATE admin_users SET role = 'admin' WHERE username = ?", (name,))
    conn.commit()


@pytest.mark.parametrize("step", ["taste", "choosing", "checking"])
def test_a_run_says_each_step_while_it_is_at_it(step, amy, model, tmdb, monkeypatch):
    monkeypatch.setattr(assist.threading, "Thread", _THREAD)
    at, go = threading.Event(), threading.Event()

    def hold():
        at.set()
        go.wait(10)

    # Where each step spends its time: the person's taste made into words, the model, TMDB.
    if step == "taste":
        made = answers._prompt
        monkeypatch.setattr(answers, "_prompt", lambda *a: (hold(), made(*a))[1])
    elif step == "choosing":
        model.meanwhile = lambda _conn: hold()
    else:
        found = tmdb.search_results_or_raise
        monkeypatch.setattr(
            tmdb, "search_results_or_raise", lambda *a, **k: (hold(), found(*a, **k))[1]
        )
    model.items = [_title("Alibi")]
    run = amy.post(f"{BASE}/ask", json={"q": Q}).json()["run_id"]
    try:
        assert at.wait(10)
        polled = amy.get(f"{BASE}/runs/{run}").json()
    finally:
        go.set()
        # The run ends before what it was given is taken back.
        for _ in range(200):
            if amy.get(f"{BASE}/runs/{run}").json().get("status") != "running":
                break
            time.sleep(0.01)

    assert (polled["status"], polled["stage"]) == ("running", step)


def test_an_ai_with_no_key_says_so_to_an_admin(people, conn):
    _admin(conn)
    conn.execute("DELETE FROM settings WHERE key = 'openai_api_key'")
    conn.commit()

    body = people["amy"].get(BASE).json()

    assert (body["available"], body["why"]) == (False, "no_key")
    assert people["amy"].post(f"{BASE}/ask", json={"q": Q}).json()["why"] == "no_key"


def test_a_viewer_is_not_told_why_the_ai_is_off(people, conn):
    conn.execute("DELETE FROM settings WHERE key = 'openai_api_key'")
    conn.commit()

    body = people["amy"].get(BASE).json()

    assert (body["available"], body["why"]) == (False, None)
    refused = people["amy"].post(f"{BASE}/ask", json={"q": Q}).json()
    assert refused["error"] == "unavailable" and "why" not in refused


def test_an_ai_whose_key_was_refused_says_so(amy, conn, monkeypatch):
    _admin(conn)
    monkeypatch.setattr(openai_client, "_refused_key", openai_client._digest("sk-test"))
    monkeypatch.setattr(openai_client, "_refused_at", time.monotonic())

    assert amy.get(BASE).json()["why"] == "refused"
