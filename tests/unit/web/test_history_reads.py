"""Who may read what of the history, and how its pages follow each other."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from mediaman.core.audit import log_audit, security_event
from mediaman.scanner.repository.audit import AuditRow
from mediaman.web.repository.history import (
    BadCursor,
    Filters,
    Play,
    cursor_of,
    event_for,
    history_page,
    parse_filters,
    people,
    viewer_for,
)
from mediaman.web.routes._history_view import own_item, play_item
from mediaman.web.routes.history import _build_item
from tests.helpers.factories import insert_audit_log, insert_media_item

NOON = datetime(2026, 3, 1, 12, tzinfo=UTC)
CREATED = (NOON - timedelta(days=30)).isoformat()


def _at(minutes: float) -> str:
    return (NOON + timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def db(conn):
    conn.executemany(
        "INSERT INTO admin_users (username, password_hash, created_at, role) VALUES (?, 'x', ?, ?)",
        [("root", CREATED, "admin"), ("ana", CREATED, "viewer"), ("bob", CREATED, "viewer")],
    )
    conn.commit()
    return conn


def _id(db, name):
    return db.execute("SELECT id FROM admin_users WHERE username = ?", (name,)).fetchone()[0]


def _event(db, action="snoozed", item="m", *, owner=None, minutes=0, actor=None, detail=""):
    cursor = db.execute(
        "INSERT INTO audit_log (media_item_id, action, detail, created_at, actor, actor_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (item, action, detail, _at(minutes), actor or owner, owner and _id(db, owner)),
    )
    return cursor.lastrowid


def _read(db, name, filters=None, **kw):
    return history_page(db, viewer_for(db, name), filters or Filters(), **kw)


def _ids(page):
    return [e.ref if isinstance(e, Play) else e.id for e in page.entries]


def _walk(db, name, filters=None, per_page=2, between=lambda: None):
    seen, cursor = [], None
    while True:
        page = _read(db, name, filters, cursor=cursor, per_page=per_page)
        seen += _ids(page)
        assert len(seen) < 50, seen
        cursor = page.next_cursor
        if cursor is None:
            return seen
        between()


def _progress(db, name, key, minutes, *, item=None, show=None, watched=0, seen_at=None, pos=600):
    db.execute(
        "INSERT INTO watch_progress (username, rating_key, media_item_id, show_rating_key,"
        " position_s, duration_s, watched, updated_at, seen_at) VALUES (?, ?, ?, ?, ?, 3600, ?, ?, ?)",
        (name, key, item or key, show, pos, watched, _at(minutes), seen_at),
    )


def _plex(db, name, key, minutes, *, title="Dune", show=None, episode=None):
    db.execute(
        "INSERT OR IGNORE INTO plex_links (username, plex_account_id, server_account_id,"
        " plex_name, server_id, linked_at) VALUES (?, ?, ?, ?, 's', ?)",
        (name, f"p{name}", f"s{name}", name, CREATED),
    )
    db.execute(
        "INSERT INTO plex_plays (username, history_key, media_type, title, show_title,"
        " show_rating_key, season_number, episode_number, rating_key, viewed_at)"
        " VALUES (?, ?, ?, ?, ?, 's1', 1, ?, ?, ?)",
        (name, f"h{key}", "episode" if show else "movie", title, show, episode, key, _at(minutes)),
    )


# --- what is written ---


def test_an_event_holds_the_id_of_the_account_that_did_it(db):
    log_audit(db, "m", "snoozed", "Kept", actor="ana")
    security_event(db, event="login.success", actor="ana", ip="1.2.3.4")
    rows = db.execute("SELECT actor_id FROM audit_log").fetchall()
    assert [r[0] for r in rows] == [_id(db, "ana")] * 2


@pytest.mark.parametrize(
    ("event", "actor"),
    [("login.failed", "ana"), ("subscriber.opted_out", "ana"), ("user.unlocked", "cli")],
)
def test_a_name_nobody_signed_in_with_is_no_account(db, event, actor):
    db.execute(
        "INSERT INTO admin_users (username, password_hash, created_at, role)"
        " VALUES ('cli', 'x', ?, 'viewer')",
        (CREATED,),
    )
    security_event(db, event=event, actor=actor)
    assert db.execute("SELECT actor_id FROM audit_log").fetchone()[0] is None
    assert _ids(_read(db, actor)) == []


# --- who sees what ---


def test_an_admin_sees_every_row_and_anyone_else_only_their_own(db):
    ana, bob, system = (_event(db, owner="ana"), _event(db, owner="bob"), _event(db, "deleted"))
    assert _ids(_read(db, "root")) == [system, bob, ana]
    assert _ids(_read(db, "ana")) == [ana]
    assert _read(db, "ana").total == 1


def test_another_persons_event_cannot_be_opened_or_filtered_to(db):
    bob = _event(db, owner="bob", item="Dune")
    ana = _event(db, owner="ana")
    viewer = viewer_for(db, "ana")
    assert event_for(db, viewer, bob) is None
    assert event_for(db, viewer, ana).id == ana
    assert event_for(db, viewer_for(db, "root"), bob).id == bob
    for filters in (Filters(user="bob"), Filters(q="Dune"), Filters(q="bob")):
        assert _ids(_read(db, "ana", filters)) == []
    assert people(db, viewer) == ["ana"]


def test_an_old_row_is_a_persons_by_name_only_from_when_their_account_was_made(db):
    old = insert_audit_log(db, action="snoozed", actor="ana", created_at=_at(-60 * 24 * 40))
    own = insert_audit_log(db, action="snoozed", actor="ana", created_at=_at(0))
    insert_audit_log(db, action="sec:login.failed", actor="ana", created_at=_at(1))
    assert _ids(_read(db, "ana")) == [own]
    assert event_for(db, viewer_for(db, "ana"), old) is None


def test_a_row_of_a_deleted_account_is_not_the_next_ones_of_its_name(db):
    _event(db, owner="ana", minutes=5)
    db.execute("DELETE FROM admin_users WHERE username = 'ana'")
    db.execute(
        "INSERT INTO admin_users (username, password_hash, created_at, role)"
        " VALUES ('ana', 'x', ?, 'viewer')",
        (CREATED,),
    )
    assert _ids(_read(db, "ana")) == []


def test_a_persons_own_details_hold_nothing_of_anyone_else(db):
    detail = 'actor=ana ip=10.0.0.1 {"new_username":"bob","email":"bob@example.com","other_ip":"9.9.9.9"}'
    _event(db, "sec:user.created", "_security", owner="ana", detail=detail)
    _event(db, "re_downloaded", owner="ana", detail="Re-downloaded by bob@example.com")
    items = [own_item(_build_item(e), "ana") for e in _read(db, "ana").entries]
    text = json.dumps(items)
    assert "bob" not in text
    assert "9.9.9.9" not in text
    assert items[1]["ip"] == "10.0.0.1"
    assert set(_build_item(_read(db, "ana").entries[0])) == set(items[0])


# --- filters ---


def test_an_admin_finds_rows_by_text_person_action_title_and_date(db):
    insert_media_item(db, id="d", title="Dune")
    dune = _event(db, owner="ana", item="d")
    gone = _event(db, "deleted", item="z", minutes=1, detail="Deleted 'Heat'")
    login = _event(db, "sec:login.success", "_security", owner="bob", minutes=60 * 24)
    for filters, expected in [
        (parse_filters(q="dune"), [dune]),
        (parse_filters(title="heat"), [gone]),
        (parse_filters(q="BOB"), [login]),
        (parse_filters(user="ana"), [dune]),
        (parse_filters(action="kept"), [dune]),
        (parse_filters(action="security"), [login]),
        (parse_filters(since="2026-03-02"), [login]),
        (parse_filters(until="2026-03-01"), [gone, dune]),
        (parse_filters(since="not a date"), [login, gone, dune]),
    ]:
        page = _read(db, "root", filters)
        assert (_ids(page), page.total) == (expected, len(expected)), filters


def test_a_wildcard_in_a_search_is_only_itself(db):
    hundred = _event(db, owner="ana", item="100% Wolf")
    _event(db, owner="ana", item="1000 Wolf")
    for text in ("100%", "0% w"):
        assert _ids(_read(db, "root", Filters(title=text))) == [hundred]
    assert _ids(_read(db, "root", Filters(q="_"))) == []
    assert _ids(_read(db, "root", Filters(q="\\"))) == []


def test_a_search_reads_no_address_in_a_security_row(db):
    _event(db, "sec:login.success", "_security", owner="ana", detail="actor=ana ip=10.0.0.1")
    assert _ids(_read(db, "ana", Filters(q="10.0.0"))) == []
    assert _ids(_read(db, "root", Filters(q="10.0.0"))) == []


# --- pages ---


def test_new_rows_neither_skip_nor_double_a_row_between_pages(db):
    insert_media_item(db, id="mv", title="Dune")
    events = [_event(db, owner="ana", minutes=m) for m in (0, 0, 0, 1, 2)]
    legacy = insert_audit_log(db, action="snoozed", actor="ana", created_at=_at(0))
    _progress(db, "ana", "mv", 0)
    ref = "w:" + str(db.execute("SELECT rowid FROM watch_progress").fetchone()[0])
    expected = [events[4], events[3], legacy, events[2], events[1], events[0], ref]
    for name in ("ana", "root"):
        for per_page in (1, 2, 3):
            assert _walk(db, name, per_page=per_page) == expected
    add = lambda: _event(db, owner="ana", minutes=9)  # noqa: E731
    assert _walk(db, "ana", between=add) == expected


def test_a_page_can_start_at_an_event(db):
    first, second = _event(db, owner="ana"), _event(db, owner="ana", minutes=1)
    _event(db, owner="ana", minutes=2)
    cursor = cursor_of(event_for(db, viewer_for(db, "ana"), second))
    assert _ids(_read(db, "ana", cursor=cursor)) == [second, first]


@pytest.mark.parametrize(
    "value",
    ["x", "!!", "a" * 400, *([1, 2], {}, ["t", 1, "x"], ["t", 0, 4], [1, 1, 1], ["t", 1, 2**70])],
)
def test_a_cursor_nobody_gave_out_is_refused(db, value):
    if not isinstance(value, str):
        value = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    with pytest.raises(BadCursor):
        _read(db, "root", cursor=value)


# --- plays ---


def test_plays_are_everyones_for_an_admin_and_else_only_ones_own(db):
    _plex(db, "ana", "1", 0)
    _plex(db, "bob", "2", 1, title="Heat")
    plays = Filters(action="plays")
    event = _event(db, owner="ana", minutes=2)
    assert [(e.username, e.title) for e in _read(db, "root", plays).entries] == [
        ("bob", "Heat"),
        ("ana", "Dune"),
    ]
    assert [e.username for e in _read(db, "ana", plays).entries] == ["ana"]
    assert _read(db, "ana", plays).total == 1
    assert [e.username for e in _read(db, "root", Filters(action="plays", user="bob")).entries] == [
        "bob"
    ]
    assert _read(db, "ana", Filters(action="plays", user="bob")).entries == []
    assert _ids(_read(db, "ana"))[0] == event


def test_one_sitting_with_a_show_is_one_line(db):
    insert_media_item(
        db, id="se", title="Season 1", show_title="Lost", media_type="tv", show_rating_key="s1"
    )
    db.execute(
        "UPDATE media_items SET season_number = 1, episode_keys = ? WHERE id = 'se'",
        (json.dumps([{"key": "e3", "number": 3}]),),
    )
    _plex(db, "ana", "e1", 0, title="Pilot", show="Lost", episode=1)
    _plex(db, "ana", "e2", 50, title="Pilot 2", show="lost", episode=2)
    _progress(db, "ana", "e3", 100, item="se", show="s1")
    _plex(db, "ana", "e9", 60 * 24, title="Late", show="Lost", episode=9)
    late, sitting = _read(db, "ana", Filters(action="plays")).entries
    assert [p.episode_number for p in late.parts] == [9]
    assert late.finished
    assert [(p.season_number, p.episode_number) for p in sitting.parts] == [(1, 1), (1, 2), (1, 3)]
    assert (sitting.started_at, sitting.created_at, sitting.finished) == (_at(0), _at(100), False)
    assert play_item(sitting)["summary"] == "3 episodes"


def test_a_film_played_here_and_told_by_plex_is_one_finished_line(db):
    insert_media_item(db, id="mv", title="Dune")
    _plex(db, "ana", "mv", 0)
    _progress(db, "ana", "mv", 2)
    db.execute("INSERT INTO played_titles VALUES ('ana', 'movie', 'Dune', NULL, ?)", (_at(2),))
    (play,) = _read(db, "ana", Filters(action="plays")).entries
    assert play.finished
    assert len(play.parts) == 1


def test_a_title_gone_from_the_library_is_still_a_play(db):
    db.execute("INSERT INTO played_titles VALUES ('ana', 'movie', 'Heat', NULL, ?)", (_at(0),))
    (play,) = _read(db, "ana", Filters(action="plays")).entries
    assert (play.title, play.finished) == ("Heat", False)


def test_a_mark_made_by_hand_is_not_a_play(db):
    for key, title in (("mv", "Dune"), ("m2", "Heat")):
        insert_media_item(db, id=key, title=title)
    _progress(db, "ana", "mv", 0, watched=1, seen_at=_at(600))
    _progress(db, "ana", "m2", 0, watched=1, seen_at=_at(1), pos=0)
    db.execute(
        "UPDATE watch_progress SET updated_at = '1970-01-01T00:00:00+00:00' WHERE rating_key = 'm2'"
    )
    (play,) = _read(db, "ana", Filters(action="plays")).entries
    assert (play.title, play.finished) == ("Dune", False)


def test_plays_of_a_person_whose_link_is_gone_are_not_shown(db):
    _plex(db, "ana", "1", 0)
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DELETE FROM plex_links")
    assert _read(db, "root").entries == []


# --- speed ---


@pytest.mark.parametrize(
    ("name", "index"),
    [
        ("root", "idx_audit_log_created"),
        ("ana", "idx_audit_log_actor_id_created"),
        ("ana", "idx_audit_log_actor "),
    ],
)
def test_a_page_is_read_through_an_index(db, name, index):
    statements = []
    db.set_trace_callback(statements.append)
    _read(db, name, cursor=cursor_of(AuditRow(9, "", "", None, None, _at(0), None, None, None)))
    db.set_trace_callback(None)
    plans = [
        row[3] + " "
        for sql in statements
        if "FROM audit_log" in sql
        for row in db.execute("EXPLAIN QUERY PLAN " + sql)
    ]
    assert any(index in plan for plan in plans), plans
    assert not any(plan.startswith("SCAN al") and "INDEX" not in plan for plan in plans), plans
