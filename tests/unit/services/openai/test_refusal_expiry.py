"""A refused key is remembered for an hour, then asked about again."""

from __future__ import annotations

import pytest

from mediaman.db import init_db
from mediaman.services.infra import SafeHTTPError
from mediaman.services.openai import client as client_mod
from mediaman.services.openai.client import call_openai, key_refused
from tests.helpers.factories import insert_settings

HOUR = 3600.0


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


class Clock:
    def __init__(self) -> None:
        self.now = 50_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(client_mod, "monotonic", clock)
    monkeypatch.setattr(client_mod, "_refused_key", None)
    return clock


def _set_key(conn, key):
    conn.execute("DELETE FROM settings WHERE key = 'openai_api_key'")
    insert_settings(conn, openai_api_key=key)


def _answers(monkeypatch, *answers):
    queue = list(answers)

    def post(*_a, **_k):
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(client_mod._OPENAI_CLIENT, "post", post)


def _refusal():
    return SafeHTTPError(401, "", "https://api.openai.com")


def test_a_refusal_is_known_for_the_first_hour(conn, clock, monkeypatch):
    _set_key(conn, "sk-test")
    _answers(monkeypatch, _refusal())
    call_openai("p", conn)
    clock.now += HOUR - 1
    assert key_refused(conn) is True


def test_a_refusal_is_forgotten_after_the_hour(conn, clock, monkeypatch):
    _set_key(conn, "sk-test")
    _answers(monkeypatch, _refusal())
    call_openai("p", conn)
    clock.now += HOUR
    assert key_refused(conn) is False


def test_a_new_refusal_after_the_hour_is_known_for_another_hour(conn, clock, monkeypatch):
    _set_key(conn, "sk-test")
    _answers(monkeypatch, _refusal(), _refusal())
    call_openai("p", conn)
    clock.now += HOUR + 10
    assert key_refused(conn) is False
    call_openai("p", conn)
    assert key_refused(conn) is True
    clock.now += HOUR - 1
    assert key_refused(conn) is True
    clock.now += 2
    assert key_refused(conn) is False


def test_a_good_answer_inside_the_hour_clears_the_refusal(conn, clock, monkeypatch, fake_response):
    _set_key(conn, "sk-test")
    _answers(monkeypatch, _refusal(), fake_response(json_data={"output": []}))
    call_openai("p", conn)
    clock.now += 60
    call_openai("p", conn)
    assert key_refused(conn) is False


def test_a_changed_key_is_not_refused_inside_the_hour(conn, clock, monkeypatch):
    _set_key(conn, "sk-old")
    _answers(monkeypatch, _refusal())
    call_openai("p", conn)
    _set_key(conn, "sk-new")
    assert key_refused(conn) is False
