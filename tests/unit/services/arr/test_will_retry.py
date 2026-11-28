"""What a download that needs attention says mediaman is doing about it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediaman.db import init_db
from mediaman.services.arr.finding_copy import fixing_words, will_retry
from tests.helpers.factories import insert_settings

_NOW = datetime(2026, 1, 10, 12, tzinfo=UTC)
_STUCK = _NOW.isoformat()
_NO_FILES = "No files found are eligible for import in /downloads/completed/tv/Show.S01E01"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("mediaman.services.arr.finding_copy.now_utc", lambda: _NOW)
    conn = init_db(str(tmp_path / "mediaman.db"))
    yield conn
    conn.close()


def _row(conn, key: str, tries: int, stuck_since: str | None, **more: str) -> None:
    columns = ", ".join(["key", "stuck_since", "tries", *more])
    marks = ", ".join("?" * (3 + len(more)))
    with conn:
        conn.execute(
            f"INSERT INTO arr_import_retries ({columns}) VALUES ({marks})",
            (key, stuck_since, tries, *more.values()),
        )


def test_a_show_retries_its_stuck_season_though_another_season_was_tried_often(conn):
    _row(conn, "sonarr:5:1", 2, None)
    _row(conn, "sonarr:5:2", 0, _STUCK)

    assert will_retry(conn, "sonarr:5:", ["Not a sample"])


def test_a_season_tried_many_times_before_is_still_retried(conn):
    _row(conn, "sonarr:5:2", 9, _STUCK)

    assert fixing_words(conn, "sonarr:5:", ["Sample"]) == "Fixing — finding another copy"


def test_a_download_sonarr_holds_for_a_while_is_not_said_to_be_retried(conn):
    assert not will_retry(conn, "sonarr:5:2", ["Episode has a TBA title and recently aired"])


def test_a_fault_of_the_setup_is_left_for_an_admin(conn):
    assert fixing_words(conn, "sonarr:5:2", ["Not enough free space"]) == ""


def test_nothing_is_said_while_the_setting_is_off(conn):
    insert_settings(conn, import_retry_enabled="false")
    assert fixing_words(conn, "sonarr:5:2", [_NO_FILES]) == ""


@pytest.mark.parametrize(
    ("plan", "words"),
    [
        ("rename", "Fixing — renaming files"),
        ("import", "Fixing — importing the files"),
        ("replace", "Fixing — finding another copy"),
        (None, "Fixing — finding another copy"),
        ("", ""),
    ],
)
def test_the_card_says_the_step_planned_for_the_title(conn, plan, words):
    _row(conn, "sonarr:5:2", 0, _STUCK, fix_plan=plan)

    assert fixing_words(conn, "sonarr:5:2", [_NO_FILES]) == words


def test_a_title_stuck_for_a_week_says_so_and_that_it_is_still_tried(conn):
    first = (_NOW - timedelta(days=8)).isoformat()
    _row(conn, "radarr:9", 4, _STUCK, first_stuck_at=first)

    assert fixing_words(conn, "radarr:9", ["Sample"]) == "Stuck for 8 days — still trying daily"
