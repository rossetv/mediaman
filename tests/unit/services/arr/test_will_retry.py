"""Whether a download that needs attention says mediaman will look for another copy of it."""

from __future__ import annotations

import pytest

from mediaman.db import init_db
from mediaman.services.arr.finding_copy import will_retry

_STUCK = "2026-01-10T12:00:00+00:00"


@pytest.fixture
def conn(tmp_path):
    conn = init_db(str(tmp_path / "mediaman.db"))
    yield conn
    conn.close()


def _row(conn, key: str, tries: int, stuck_since: str | None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO arr_import_retries (key, stuck_since, tries) VALUES (?, ?, ?)",
            (key, stuck_since, tries),
        )


def test_a_show_retries_its_stuck_season_though_another_season_spent_its_tries(conn):
    _row(conn, "sonarr:5:1", 2, None)
    _row(conn, "sonarr:5:2", 0, _STUCK)

    assert will_retry(conn, "sonarr:5:", ["Not a sample"])


def test_a_show_whose_stuck_season_spent_its_tries_is_not_retried(conn):
    _row(conn, "sonarr:5:1", 0, None)
    _row(conn, "sonarr:5:2", 2, _STUCK)

    assert not will_retry(conn, "sonarr:5:", ["Not a sample"])


def test_a_download_sonarr_holds_for_a_while_is_not_said_to_be_retried(conn):
    assert not will_retry(conn, "sonarr:5:2", ["Episode has a TBA title and recently aired"])
