"""A question being answered says which step it is at, and an AI not offered says why."""

# ruff: noqa: F811

from __future__ import annotations

import threading
import time

from mediaman.services.openai import client as openai_client
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


def test_a_run_says_it_is_choosing_titles_while_the_model_answers(amy, model, monkeypatch):
    monkeypatch.setattr(assist.threading, "Thread", _THREAD)
    asked, go = threading.Event(), threading.Event()

    def meanwhile(_conn):
        asked.set()
        go.wait(10)

    model.meanwhile = meanwhile
    model.items = [_title("Alibi")]
    run = amy.post(f"{BASE}/ask", json={"q": Q}).json()["run_id"]
    try:
        assert asked.wait(10)
        polled = amy.get(f"{BASE}/runs/{run}").json()
    finally:
        go.set()

    assert (polled["status"], polled["stage"]) == ("running", "choosing")


def test_an_ai_with_no_key_says_so(people, conn):
    conn.execute("DELETE FROM settings WHERE key = 'openai_api_key'")
    conn.commit()

    body = people["amy"].get(BASE).json()

    assert (body["available"], body["why"]) == (False, "no_key")
    assert people["amy"].post(f"{BASE}/ask", json={"q": Q}).json()["why"] == "no_key"


def test_an_ai_whose_key_was_refused_says_so(amy, monkeypatch):
    monkeypatch.setattr(openai_client, "_refused_key", openai_client._digest("sk-test"))
    monkeypatch.setattr(openai_client, "_refused_at", time.monotonic())

    assert amy.get(BASE).json()["why"] == "refused"
