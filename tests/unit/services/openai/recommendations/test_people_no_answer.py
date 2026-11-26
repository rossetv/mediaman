"""A scan's own picks when OpenAI gives no answer: one call, one line, and nobody held back
for a key that was refused."""

from __future__ import annotations

import logging

import pytest
import requests

from mediaman.db import init_db
from mediaman.services.infra import SafeHTTPError
from mediaman.services.openai import client as openai_client
from mediaman.services.openai.recommendations import personal
from tests.helpers.factories import insert_settings
from tests.unit.services.openai.recommendations.test_personal import _KEY, NOW, _person, _plays


@pytest.fixture
def conn(db_path, freezer, monkeypatch):
    freezer.move_to(NOW)
    conn = init_db(str(db_path))
    for name in ("amy", "bob"):
        _person(conn, name)
        _plays(conn, name, "A", "B", "C", "D", "E")
    insert_settings(conn, openai_api_key="sk-test")
    monkeypatch.setattr(openai_client, "_refused_key", None)
    yield conn
    conn.close()


@pytest.fixture
def openai(monkeypatch):
    """OpenAI failing with ``error``; ``calls`` counts what was sent."""

    class Failing:
        error: Exception = SafeHTTPError(401, "", "https://api.openai.com")
        calls = 0

    def post(*_a, **_k):
        Failing.calls += 1
        raise Failing.error

    monkeypatch.setattr(openai_client._OPENAI_CLIENT, "post", post)
    return Failing


def _statuses(conn):
    return [r[0] for r in conn.execute("SELECT status FROM user_pick_runs")]


def test_a_refused_key_is_asked_once_and_said_in_one_line(conn, openai, caplog):
    with caplog.at_level(logging.INFO, logger="mediaman"):
        assert personal.refresh_people(conn, None, secret_key=_KEY) == 0
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert openai.calls == 1
    assert len(warnings) == 1 and "401" in warnings[0]


def test_nobody_waits_a_day_for_a_key_that_was_refused(conn, openai):
    personal.refresh_people(conn, None, secret_key=_KEY)
    assert "failed" not in _statuses(conn)
    assert sorted(personal.eligible_people(conn, NOW)) == ["amy", "bob"]


def test_a_key_known_refused_is_not_sent_again(conn, openai):
    personal.refresh_people(conn, None, secret_key=_KEY)
    personal.refresh_people(conn, None, secret_key=_KEY)
    assert openai.calls == 1


def test_a_timeout_stops_the_scan_at_the_first_person(conn, openai):
    openai.error = requests.Timeout("slow")
    assert personal.refresh_people(conn, None, secret_key=_KEY) == 0
    assert openai.calls == 1
