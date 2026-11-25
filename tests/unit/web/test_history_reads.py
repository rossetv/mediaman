"""Who may read what of the history, and how its pages follow each other."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from mediaman.core.audit import log_audit, security_event
from mediaman.scanner.repository.audit import AuditRow
from mediaman.web.repository.history import (
    GROUPS,
    SYSTEM,
    BadCursor,
    Filters,
    Play,
    Who,
    action_counts,
    cursor_of,
    event_for,
    history_page,
    parse_filters,
    people,
    title_counts,
    user_counts,
    viewer_for,
    who_of,
    whose,
)
from mediaman.web.routes._history_view import own_item, play_item
from mediaman.web.routes.history import ACTION_BADGE_CLASS, SECURITY_EVENT_LABELS, _build_item
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
    assert people(db, viewer) == [Who("account", "ana")]


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


def test_a_stored_key_named_like_a_shown_line_is_not_shown(db):
    detail = 'actor=ana ip=10.0.0.1 {"event":"bob","time":"bob","title":"bob","event_id":"bob"}'
    _event(db, "sec:user.created", "_security", owner="ana", detail=detail)
    (event,) = _read(db, "ana").entries
    assert "bob" not in json.dumps(own_item(_build_item(event), "ana"))


def test_a_search_of_ones_own_does_not_read_the_stored_text_or_the_names(db):
    _event(db, owner="ana", detail="Kept for bob", item="m")
    for filters in (Filters(q="bob"), Filters(title="bob"), Filters(q="ana")):
        assert _ids(_read(db, "ana", filters)) == []
    assert len(_ids(_read(db, "root", Filters(q="ana")))) == 1
    _plex(db, "ana", "1", 0)
    assert _ids(_read(db, "ana", Filters(q="ana"))) == []


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
        assert _ids(_read(db, "root", Filters(q=text))) == [hundred]
    assert _ids(_read(db, "root", Filters(q="_"))) == []
    assert _ids(_read(db, "root", Filters(q="\\"))) == []


def test_a_search_reads_no_address_in_a_security_row(db):
    _event(db, "sec:login.success", "_security", owner="ana", detail="actor=ana ip=10.0.0.1")
    assert _ids(_read(db, "ana", Filters(q="10.0.0"))) == []
    assert _ids(_read(db, "root", Filters(q="10.0.0"))) == []


_GROUPED = {
    "downloads": ["downloaded", "re_downloaded", "sec:auto_abandon.fired"],
    "keeps": [
        "protected",
        "protected_forever",
        "kept",
        "kept_show",
        "snoozed",
        "unprotected",
        "unkept",
        "removed_show_keep",
    ],
    "deletions": [
        "scheduled_deletion",
        "deleted",
        "delete_failed",
        "delete_failed_permanent",
        "delete_refused",
        "delete_root_unavailable",
        "delete_root_available",
        "delete_awaiting_scan",
        "delete_incomplete",
        "delete_folders_failed",
        "dry_run_skip",
        "media_missing",
        "unmonitor_skipped",
    ],
    "sign_ins": ["sec:session.revoke_one", "sec:password.weak_detected", "sec:plex.link_started"],
    "people": ["sec:user.created", "sec:user.delete.rate_limited", "sec:user.unlocked"],
    "settings": ["sec:settings.write.throttled", "sec:aes.canary_failed"],
    "scans": ["sec:scan.cleared", "sec:library.sync.failed", "library_left_out", "scanned"],
    "newsletter": ["sec:newsletter.sent", "sec:subscriber.opted_out"],
}


@pytest.mark.parametrize("group", _GROUPED)
def test_a_group_holds_its_own_actions_and_no_others(db, group):
    for actions in _GROUPED.values():
        for action in actions:
            _event(db, action)
    _plex(db, "ana", "1", 0)
    page = _read(db, "root", Filters(action=group), per_page=100)
    assert sorted(e.action for e in page.entries) == sorted(_GROUPED[group])
    assert page.total == len(_GROUPED[group])


def test_every_known_action_is_in_exactly_one_group(db):
    known = {f"sec:{e}" for e in SECURITY_EVENT_LABELS} | set(ACTION_BADGE_CLASS)
    for action in known:
        _event(db, action)
    found = [
        e.action
        for group in GROUPS
        for e in _read(db, "root", Filters(action=group), per_page=100).entries
    ]
    assert sorted(found) == sorted(known)


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


@pytest.mark.parametrize("table", ["watch_progress", "played_titles"])
def test_what_another_person_played_here_is_not_shown(db, table):
    insert_media_item(db, id="mv", title="Dune")
    for name in ("ana", "bob"):
        if table == "watch_progress":
            _progress(db, name, "mv", 0)
        else:
            db.execute(
                "INSERT INTO played_titles VALUES (?, 'movie', 'Dune', NULL, ?)", (name, _at(0))
            )
    page = _read(db, "ana")
    assert ([e.username for e in page.entries], page.total) == (["ana"], 1)
    assert _read(db, "root").total == 2


def test_pages_of_plays_over_years_hold_each_sitting_once_and_whole(db):
    day = 60 * 24
    # A long run of one show, whose end is on one page's edge, and sparse plays before it.
    run = [-200 * 60 + i * 200 for i in range(61)]
    for i, minutes in enumerate(run):
        _plex(db, "ana", f"r{i}", minutes, title=f"E{i}", show="Lost", episode=i)
    old = [-day * d for d in (29, 30, 31, 179, 400, 900)]
    for i, minutes in enumerate(old):
        _plex(db, "ana", f"o{i}", minutes, title="Dune")
    expected = [(_at(run[-1]), 61), *((_at(m), 1) for m in sorted(old, reverse=True))]
    for per_page in (1, 2, 100):
        seen, cursor = [], None
        while True:
            page = _read(db, "ana", cursor=cursor, per_page=per_page)
            seen += [(p.created_at, len(p.parts)) for p in page.entries]
            cursor = page.next_cursor
            if cursor is None:
                break
            assert len(seen) < 50
        assert seen == expected
    assert _read(db, "ana").total == len(expected)


def test_a_sitting_still_going_at_the_end_of_the_dates_is_listed_by_its_end(db):
    for i, minutes in enumerate((0, 200)):
        _plex(db, "ana", f"r{i}", minutes, title=f"E{i}", show="Lost", episode=i)
    for i in (1, 2, 3):
        _plex(db, "ana", f"o{i}", -i * 600)
    page = _read(db, "ana", Filters(until=NOON + timedelta(minutes=100)), per_page=1)
    assert ([e.created_at for e in page.entries], page.total) == ([_at(-600)], 3)


def test_a_sitting_begun_before_the_span_first_read_is_whole(db):
    day = 60 * 24
    _plex(db, "ana", "new", 0)
    for i in range(21):
        _plex(db, "ana", f"r{i}", -31 * day + i * 200, title=f"E{i}", show="Lost", episode=i)
    _plex(db, "ana", "old", -30 * day + 1)
    (_, sitting) = _read(db, "ana", per_page=2).entries
    assert len(sitting.parts) == 21


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
    assert play_item(sitting)["episode_range"] == "S1E1 to E3"
    assert play_item(sitting)["summary"] == "S1E1 to E3 · stopped at 10 min of 1 h"


def test_a_film_played_here_and_told_by_plex_is_one_finished_line(db):
    insert_media_item(db, id="mv", title="Dune")
    _plex(db, "ana", "mv", 0)
    _progress(db, "ana", "mv", 2)
    db.execute("INSERT INTO played_titles VALUES ('ana', 'movie', 'Dune', NULL, ?)", (_at(2),))
    (play,) = _read(db, "ana", Filters(action="plays")).entries
    assert play.finished
    assert len(play.parts) == 1


def test_a_film_left_half_way_says_how_far_and_how_long(db):
    insert_media_item(db, id="mv", title="Dune")
    db.execute("UPDATE media_items SET year = 2021 WHERE id = 'mv'")
    _progress(db, "ana", "mv", 0, pos=1200)
    (play,) = _read(db, "ana", Filters(action="plays")).entries
    item = play_item(play)
    assert item["progress"] == {
        "position_s": 1200,
        "duration_s": 3600,
        "fraction": 0.3333,
        "text": "20 min of 1 h",
    }
    assert (item["year"], item["runtime_text"]) == (2021, "1 h")


def test_a_film_played_in_plex_has_its_stored_runtime_and_no_place(db):
    insert_media_item(db, id="mv", title="Dune")
    db.execute("UPDATE media_items SET tmdb_id = 7")
    db.execute(
        "INSERT INTO title_meta (id_type, id, details, fetched_at)"
        " VALUES ('movie', 7, '{\"runtime\":155}', 'x')"
    )
    _plex(db, "ana", "mv", 0)
    (play,) = _read(db, "ana", Filters(action="plays")).entries
    item = play_item(play)
    assert (item["runtime_text"], item["progress"]) == ("2 h 35 min", None)


def test_episodes_not_in_a_row_are_only_counted(db):
    for minutes, episode in ((0, 1), (50, 4)):
        _plex(db, "ana", f"e{episode}", minutes, title="E", show="Lost", episode=episode)
    (play,) = _read(db, "ana", Filters(action="plays")).entries
    item = play_item(play)
    assert (item["episode_range"], item["summary"]) == (None, "2 episodes")


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


# --- whose ---


def _recreate(db, name):
    db.execute("DELETE FROM admin_users WHERE username = ?", (name,))
    db.execute(
        "INSERT INTO admin_users (username, password_hash, created_at, role)"
        " VALUES (?, 'x', ?, 'viewer')",
        (name, _at(100)),
    )


def test_former_users_and_mediaman_can_be_filtered_by(db):
    _event(db, owner="bob", minutes=1)
    petra = _event(db, actor="petra", minutes=2)
    system = _event(db, "deleted", minutes=3)
    failed = _event(db, "sec:login.failed", "_security", actor="ghost", minutes=4)
    found = people(db, viewer_for(db, "root"))
    assert found == [
        *(Who("account", n) for n in ("ana", "bob", "root")),
        Who("former", "petra"),
        SYSTEM,
    ]
    assert _ids(_read(db, "root", Filters(user=Who("former", "petra").value))) == [petra]
    assert _ids(_read(db, "root", Filters(user=SYSTEM.value))) == [failed, system]


def test_a_new_account_of_a_former_users_name_is_another_choice(db):
    old = _event(db, owner="bob", minutes=1)
    old_by_name = insert_audit_log(db, action="snoozed", actor="bob", created_at=_at(2))
    _recreate(db, "bob")
    new = _event(db, owner="bob", minutes=200)
    former, account = Who("former", "bob"), Who("account", "bob")
    assert {former, account} <= set(people(db, viewer_for(db, "root")))
    for who, ids in ((former, [old_by_name, old]), (account, [new])):
        page = _read(db, "root", Filters(user=who.value))
        assert (_ids(page), page.total) == (ids, len(ids))
        assert set(whose(db, page.entries).values()) == {who}


@pytest.mark.parametrize("name", ["~mediaman", "mediaman", "~former:bob", "~", "~~x", "former:bob"])
def test_no_accounts_name_is_taken_for_a_former_user_or_mediaman(db, name):
    db.execute(
        "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, 'x', ?)",
        (name, CREATED),
    )
    _event(db, owner="bob")
    _event(db, "deleted")
    own = _event(db, owner=name, minutes=1)
    who = Who("account", name)
    assert who_of(db, who.value) == who
    assert who.value not in {SYSTEM.value, Who("former", "bob").value}
    assert _ids(_read(db, "root", Filters(user=who.value))) == [own]


def test_a_person_who_is_no_admin_cannot_filter_to_anyone_else(db):
    _event(db, owner="bob")
    _event(db, "deleted")
    _event(db, owner="ana")
    for value in ("bob", SYSTEM.value, Who("former", "bob").value, "~nothing"):
        assert _ids(_read(db, "ana", Filters(user=value))) == []
    assert len(_ids(_read(db, "ana", Filters(user="ana")))) == 1


# --- counts ---


def _many(db):
    insert_media_item(db, id="d", title="Dune")
    _event(db, owner="ana", item="d", minutes=1)
    _event(db, "deleted", item="d", minutes=2)
    _event(db, "deleted", item="z", minutes=3, detail="Deleted 'Heat'")
    _event(db, "sec:login.success", "_security", owner="bob", minutes=4)
    _event(db, actor="petra", item="d", minutes=5)
    _plex(db, "bob", "1", 600, title="dune")
    _plex(db, "ana", "2", 600, title="Alien")


@pytest.mark.parametrize(
    "filters",
    [
        Filters(),
        Filters(q="d"),
        Filters(action="keeps"),
        Filters(title="Dune"),
        Filters(user="ana"),
        Filters(user=SYSTEM.value, action="deletions"),
        Filters(since=NOON + timedelta(minutes=2)),
        Filters(title="Heat", user=SYSTEM.value),
    ],
)
@pytest.mark.parametrize("name", ["root", "ana"])
def test_each_count_is_how_many_rows_that_choice_lists(db, name, filters):
    _many(db)
    viewer = viewer_for(db, name)
    users = user_counts(db, viewer, filters)
    actions = action_counts(db, viewer, filters)
    titles = title_counts(db, viewer, filters)
    for who, count in users:
        assert count == len(_ids(_read(db, name, replace(filters, user=who.value)))), who
    for action, count in actions.items():
        assert count == len(_ids(_read(db, name, replace(filters, action=action)))), action
    for title in titles:
        page = _read(db, name, replace(filters, title=title.title))
        assert title.count == page.total == len(_ids(page)) > 0, title


def test_titles_are_listed_by_count_and_found_by_part_of_their_name(db):
    _many(db)
    root = viewer_for(db, "root")
    assert [(t.title, t.count) for t in title_counts(db, root, Filters())] == [
        ("Dune", 4),
        ("Alien", 1),
        ("Heat", 1),
    ]
    assert [t.title for t in title_counts(db, root, Filters(), "EA")] == ["Heat"]
    assert title_counts(db, root, Filters(), "%") == []
    assert len(title_counts(db, root, Filters(title="Heat"))) == 3


def test_a_menu_tells_a_person_who_is_no_admin_only_of_their_own(db):
    _many(db)
    ana = viewer_for(db, "ana")
    assert user_counts(db, ana, Filters()) == [(Who("account", "ana"), 2)]
    assert action_counts(db, ana, Filters()) == {
        "plays": 1,
        "downloads": 0,
        "keeps": 1,
        "sign_ins": 0,
    }
    assert {t.title for t in title_counts(db, ana, Filters())} == {"Dune", "Alien"}
    assert {t.count for t in title_counts(db, ana, Filters())} == {1}


def test_a_title_stored_with_spaces_around_it_is_counted_and_listed_alike(db):
    insert_media_item(db, id="d", title=" Dune  ")
    insert_media_item(db, id="t", title="\tHeat")
    _event(db, "deleted", item="d")
    _event(db, "deleted", item="e", detail="Deleted 'Dune'")
    _event(db, "deleted", item="t")
    _plex(db, "ana", "1", 0, title="Dune ")
    root = viewer_for(db, "root")
    counts = [(t.title, t.count) for t in title_counts(db, root, Filters())]
    assert dict(counts)["Dune"] == 3
    for title, count in counts:
        assert len(_ids(_read(db, "root", parse_filters(title=title)))) == count


def test_a_title_filter_is_the_whole_title(db):
    dune = _event(db, "deleted", detail="Deleted 'Dune'", item="a")
    _event(db, "deleted", detail="Deleted 'Dune: Part Two'", item="b")
    assert _ids(_read(db, "root", Filters(title="dune"))) == [dune]


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
