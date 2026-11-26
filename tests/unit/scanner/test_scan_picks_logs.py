"""What a scan logs when the shared picks get no answer from OpenAI: never a traceback for a
missing key, a refused one or a network failure; a bug in the code keeps its traceback."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
import requests

from mediaman.db import init_db
from mediaman.scanner import engine as engine_mod
from mediaman.services.infra import SafeHTTPError
from mediaman.services.openai import client as openai_client
from tests.helpers.factories import insert_settings


@pytest.fixture
def scan(db_path, monkeypatch, caplog):
    """Run a scan's follow-ups with OpenAI failing by *exc* (None: no key is set); returns
    the records of warning and above."""
    conn = init_db(str(db_path))
    for name in ("_read_plays", "_refresh_people", "_send_newsletter"):
        monkeypatch.setattr(engine_mod, name, MagicMock())
    monkeypatch.setattr(openai_client, "_refused_key", None)

    def run(exc):
        if exc is not None:
            insert_settings(conn, openai_api_key="sk-test")

            def post(*_a, **_k):
                raise exc

            monkeypatch.setattr(openai_client._OPENAI_CLIENT, "post", post)
        engine = engine_mod.ScanEngine(
            conn=conn,
            plex_client=MagicMock(),
            library_ids=[],
            library_types={},
            secret_key="k",
            min_age_days=30,
            inactivity_days=30,
        )
        with caplog.at_level(logging.DEBUG, logger="mediaman"):
            engine._run_post_scan_followups()
        return [r for r in caplog.records if r.levelno >= logging.WARNING]

    yield run
    conn.close()


def test_an_install_without_openai_logs_nothing_alarming(scan):
    assert scan(None) == []


@pytest.mark.parametrize(
    ("exc", "said"),
    [
        (SafeHTTPError(401, "", "https://api.openai.com"), "401"),
        (requests.Timeout("slow"), "timed out"),
        (requests.ConnectionError("down"), "network"),
    ],
)
def test_no_answer_from_openai_is_one_plain_line_that_says_why(scan, exc, said):
    [record] = scan(exc)
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert said in record.getMessage()


def test_a_bug_in_the_picks_keeps_its_traceback(scan, monkeypatch):
    monkeypatch.setattr(
        engine_mod, "_refresh_recommendations", MagicMock(side_effect=RuntimeError("bug"))
    )
    [record] = scan(None)
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
