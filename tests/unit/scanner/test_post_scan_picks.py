"""After a scan the shared picks, each person's own and the newsletter: none costs the next."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from mediaman.db import init_db
from mediaman.scanner import engine as engine_mod
from mediaman.services.infra import SafeHTTPError
from tests.helpers.factories import insert_settings


@pytest.fixture
def scan(db_path, monkeypatch):
    """Run the follow-ups of a scan, returning the ones called in order."""
    conn = init_db(str(db_path))
    called: list[str] = []

    def run(dry_run=False, **errors):
        for name in _ALL:
            monkeypatch.setattr(
                engine_mod,
                name,
                MagicMock(
                    side_effect=lambda *a, _n=name, **k: (
                        called.append(_n),
                        (_ for _ in ()).throw(errors[_n]) if _n in errors else None,
                    )
                ),
            )
        engine = engine_mod.ScanEngine(
            conn=conn,
            plex_client=MagicMock(),
            library_ids=[],
            library_types={},
            secret_key="k",
            min_age_days=30,
            inactivity_days=30,
            dry_run=dry_run,
        )
        engine._run_post_scan_followups()
        return called

    run.conn = conn
    yield run
    conn.close()


_ALL = ["_refresh_recommendations", "_read_plays", "_refresh_people", "_send_newsletter"]


def test_each_persons_picks_are_made_before_the_newsletter(scan):
    assert scan() == _ALL


def test_a_failure_of_the_shared_picks_does_not_cost_people_theirs(scan):
    assert scan(_refresh_recommendations=SafeHTTPError(500, "boom", "u")) == _ALL


def test_plex_plays_that_cannot_be_read_do_not_cost_people_their_picks(scan):
    assert scan(_read_plays=RuntimeError("plex")) == _ALL


def test_a_failure_of_the_own_picks_does_not_cost_the_newsletter(scan):
    assert scan(_refresh_people=sqlite3.OperationalError("locked")) == _ALL


def test_a_dry_run_makes_no_picks(scan):
    assert scan(dry_run=True) == []


def test_with_recommendations_off_no_one_has_picks_made(scan):
    insert_settings(scan.conn, suggestions_enabled="false")
    assert scan() == ["_send_newsletter"]
