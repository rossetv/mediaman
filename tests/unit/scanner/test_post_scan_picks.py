"""After a scan the shared picks, each person's own and the newsletter: none costs the next."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from mediaman.db import init_db
from mediaman.scanner import engine as engine_mod
from mediaman.services.infra import SafeHTTPError
from tests.helpers.factories import insert_settings, insert_suggestion, insert_user_pick


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


_ALL = ["_read_plays", "_refresh_recommendations", "_refresh_people", "_send_newsletter"]


def test_each_persons_picks_are_made_before_the_newsletter(scan):
    assert scan() == _ALL


@pytest.mark.parametrize(
    "error", [SafeHTTPError(500, "boom", "u"), ValueError("surrogate"), RecursionError()]
)
def test_a_failure_of_the_shared_picks_does_not_cost_people_theirs(scan, error):
    assert scan(_refresh_recommendations=error) == _ALL


def test_plex_plays_that_cannot_be_read_do_not_cost_people_their_picks(scan):
    assert scan(_read_plays=RuntimeError("plex")) == _ALL


@pytest.mark.parametrize("error", [sqlite3.OperationalError("locked"), ValueError("surrogate")])
def test_a_failure_of_the_own_picks_does_not_cost_the_newsletter(scan, error):
    assert scan(_refresh_people=error) == _ALL


def test_each_scan_removes_everyones_picks_and_runs_over_90_days_old(scan, freezer):
    freezer.move_to("2026-05-20 12:00:00")
    conn = scan.conn
    insert_settings(conn, suggestions_enabled="false")
    for name in ("amy", "bob"):
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, 'x', '2020')",
            (name,),
        )
        insert_user_pick(conn, name, title="Old", batch_id="2026-02-18")
        insert_user_pick(conn, name, title="New", batch_id="2026-02-20")
        for status, when in (("failed", "2026-02-18"), ("done", "2026-02-21")):
            conn.execute(
                "INSERT INTO user_pick_runs (username, requested_by, status, started_at)"
                " VALUES (?, 'scan', ?, ?)",
                (name, status, f"{when}T12:00:00+00:00"),
            )
    insert_suggestion(conn, title="Old", batch_id="2026-02-18")
    insert_suggestion(conn, title="New", batch_id="2026-02-20")

    scan()

    for table in ("user_picks", "suggestions"):
        assert {r[0] for r in conn.execute(f"SELECT title FROM {table}")} == {"New"}
    runs = conn.execute("SELECT username, status FROM user_pick_runs ORDER BY username")
    assert [tuple(r) for r in runs] == [("amy", "done"), ("bob", "done")]


def test_old_picks_that_cannot_be_removed_do_not_cost_the_scan(scan, monkeypatch):
    failing = MagicMock(side_effect=sqlite3.OperationalError("locked"))
    monkeypatch.setattr(engine_mod, "_prune_old_picks", failing)
    assert scan() == _ALL


def test_a_dry_run_makes_no_picks_but_still_reads_the_plays(scan):
    assert scan(dry_run=True) == ["_read_plays"]


def test_with_recommendations_off_no_one_has_picks_made_but_the_plays_are_read(scan):
    insert_settings(scan.conn, suggestions_enabled="false")
    assert scan() == ["_read_plays", "_send_newsletter"]
