"""Asking for more seasons of a show Sonarr already tracks, from search and from a pick."""

# ruff: noqa: F811

from __future__ import annotations

import copy
import json
import sqlite3
from itertools import count
from unittest.mock import MagicMock, patch

import pytest

from mediaman.db import get_db
from mediaman.scanner.repository.audit import AuditRow
from mediaman.scanner.repository.scheduled_actions import (
    fetch_pending_unmonitors,
    was_requested_since,
)
from mediaman.services.arr import _sonarr_methods
from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.spec import SONARR_SPEC
from mediaman.services.infra import SafeHTTPError
from mediaman.web.auth.password_hash import create_user, set_user_email
from mediaman.web.routes.history import _build_item
from mediaman.web.routes.recommended import api as recommended_api
from mediaman.web.routes.recommended.api import reset_download_action_limiter
from mediaman.web.routes.search import _seasons
from mediaman.web.routes.search._seasons import REQUEST_SECONDS, add_to_tracked_show
from tests.helpers.factories import (
    insert_audit_log,
    insert_media_item,
    insert_scheduled_action,
    insert_settings,
    insert_user_pick,
)
from tests.unit.web.test_search_download_rate_limit import (  # noqa: F401
    _setup_limiters,
    app,
    authed_client,
)

TVDB = 5500


def test_the_answer_is_in_words_for_someone_who_knows_nothing_of_sonarr():
    results = {n: result for n, (result, *_) in enumerate(_seasons._SAID, 2)}
    message = json.loads(_seasons._answer("Glass Coast", {1: "asked", **results}, [9]).body)[
        "message"
    ]

    assert message.startswith("Season 1 of 'Glass Coast' is asked for.")
    assert "Sonarr" not in message
    # The page says so itself, where it can be.
    assert "try again" not in message
    for number in (*results, 9):
        assert f"eason {number} " in message


def _season(number, files=0, monitored=False, total=8):
    return {
        "seasonNumber": number,
        "monitored": monitored,
        "statistics": {
            "episodeFileCount": files,
            "episodeCount": total if monitored else files,
            "totalEpisodeCount": total,
        },
    }


class Sonarr(MagicMock):
    """A Sonarr that tracks one show and does what it is asked."""

    def track(self, *seasons):
        show = {
            "id": 9,
            "tvdbId": TVDB,
            "tmdbId": 77,
            "title": "Glass Coast",
            "monitored": True,
            "seasons": list(seasons),
        }
        found = [{"tmdbId": 77, "tvdbId": TVDB}]
        self.show = show
        self.lookup_series_by_tmdb.return_value = found[0]
        self.lookup_by_tmdb_id.return_value = found
        self.get_series.side_effect = lambda: [copy.deepcopy(show)]
        self.get_series_by_tvdb.side_effect = lambda _tvdb: copy.deepcopy(show)
        self.get_series_by_id.side_effect = lambda _id: copy.deepcopy(show)
        self.remonitor_season.side_effect = self._monitor
        return self

    def _monitor(self, _series_id, number, *, guard, change, max_retries):
        guard(copy.deepcopy(self.show))
        change.series = not self.show["monitored"]
        next(s for s in self.show["seasons"] if s["seasonNumber"] == number)["monitored"] = True
        self.show["monitored"] = True

    @property
    def searched(self):
        return [c.args for c in self.remonitor_season.call_args_list]


@pytest.fixture(autouse=True)
def _reset():
    _setup_limiters()
    reset_download_action_limiter()


@pytest.fixture
def db(app):
    return app.state.db


@pytest.fixture
def viewer(app, authed_client):
    from fastapi.testclient import TestClient

    from mediaman.web.auth.session_store import create_session

    create_user(app.state.db, "amy", "password123", enforce_policy=False, role="viewer")
    set_user_email(app.state.db, "amy", "amy@example.com")
    client = TestClient(app)
    client.cookies.set("session_token", create_session(app.state.db, "amy"))
    client.headers.update({"Origin": "http://testserver"})
    return client


@pytest.fixture
def ask(db, request):
    """Ask through the route of the test: search's, or a pick's."""
    picks = []

    def post(client, sonarr, seasons, **more):
        if request.node.callspec.params.get("route") == "pick":
            if not picks:
                picks.append(
                    insert_user_pick(db, "amy", title="Glass Coast", media_type="tv", tmdb_id=77)
                )
            url, body = f"/api/watch/picks/{picks[0]}/download", {}
        else:
            url = "/api/search/download"
            body = {"media_type": "tv", "tmdb_id": 77, "title": "Glass Coast"}
        with (
            patch("mediaman.web.routes.search.download.build_sonarr_from_db", return_value=sonarr),
            patch("mediaman.web.routes.recommended.api.build_sonarr_from_db", return_value=sonarr),
        ):
            return client.post(url, json={**body, "search_seasons": seasons, **more})

    return post


def _results(resp):
    return {s["number"]: s["result"] for s in resp.json()["seasons"]}


def _audit(db):
    return [
        tuple(r)
        for r in db.execute(
            "SELECT media_item_id, action, detail, actor FROM audit_log"
            " WHERE action = 'downloaded' ORDER BY id"
        )
    ]


def _mails(db):
    return [
        tuple(r)
        for r in db.execute(
            "SELECT username, email, season_number, tvdb_id FROM download_notifications ORDER BY id"
        )
    ]


both = pytest.mark.parametrize("route", ["search", "pick"])


@both
def test_the_named_seasons_are_added_and_the_rest_left_as_they_are(viewer, db, ask, route):
    sonarr = Sonarr().track(
        _season(1, files=8, monitored=True), _season(2), _season(3), _season(4, files=2)
    )

    resp = ask(viewer, sonarr, [3, 4, 1])

    assert resp.status_code == 200
    assert _results(resp) == {1: "held", 3: "asked", 4: "asked"}
    assert sonarr.searched == [(9, 3), (9, 4)]
    sonarr.unmonitor_season.assert_not_called()
    sonarr.add_series_with_seasons.assert_not_called()


@both
def test_each_season_is_audited_and_mailed_for_by_itself(viewer, db, ask, route):
    ask(viewer, Sonarr().track(_season(3), _season(4)), [3, 4])

    assert _audit(db) == [
        (
            f"tvdb:{TVDB}:s{n}",
            "downloaded",
            f"Added season {n} of 'Glass Coast' to Sonarr by amy",
            "amy",
        )
        for n in (3, 4)
    ]
    assert _mails(db) == [("amy", "amy@example.com", n, TVDB) for n in (3, 4)]


@both
def test_without_search_seasons_a_tracked_show_is_still_a_409(viewer, ask, route):
    sonarr = Sonarr().track(_season(3))

    for seasons in (None, []):
        assert ask(viewer, sonarr, seasons, monitored_seasons=[3]).status_code == 409
    assert sonarr.searched == []


@both
def test_half_a_success_is_told_by_season_and_only_what_was_added_is_recorded(
    viewer, db, ask, route
):
    sonarr = Sonarr().track(_season(3), _season(4))

    def monitor(_series_id, number, **how):
        if number == 3:
            raise SafeHTTPError(400, "no", "sonarr/series")
        sonarr._monitor(_series_id, number, **how)

    sonarr.remonitor_season.side_effect = monitor

    resp = ask(viewer, sonarr, [3, 4])

    assert (resp.status_code, _results(resp)) == (200, {3: "failed", 4: "asked"})
    assert [row[0] for row in _audit(db)] == [f"tvdb:{TVDB}:s4"]
    assert [row[2] for row in _mails(db)] == [4]
    sonarr.unmonitor_season.assert_not_called()


@pytest.mark.parametrize("route", ["search"])
def test_sending_it_again_searches_and_records_nothing_twice(viewer, db, ask, route):
    sonarr = Sonarr().track(_season(3), _season(4))
    ask(viewer, sonarr, [3])

    resp = ask(viewer, sonarr, [3, 4])

    assert _results(resp) == {3: "already_asked", 4: "asked"}
    assert sonarr.searched == [(9, 3), (9, 4)]
    assert len(_audit(db)) == 2
    assert [row[2] for row in _mails(db)] == [3, 4]


@pytest.mark.parametrize("route", ["search"])
def test_a_request_that_failed_whole_can_be_sent_again_at_once(viewer, db, ask, route):
    sonarr = Sonarr().track(_season(3))
    sonarr.remonitor_season.side_effect = SafeHTTPError(0, "timeout", "sonarr/series")

    first = ask(viewer, sonarr, [3])
    sonarr.remonitor_season.side_effect = sonarr._monitor
    second = ask(viewer, sonarr, [3])

    assert (first.status_code, _results(first)) == (502, {3: "failed"})
    assert (first.json()["ok"], first.json()["message"]) == (False, first.json()["error"])
    assert (second.status_code, _results(second)) == (200, {3: "asked"})


@pytest.mark.parametrize("route", ["search"])
def test_once_sonarr_gives_no_answer_the_other_seasons_do_not_wait_for_it_too(viewer, ask, route):
    sonarr = Sonarr().track(_season(3), _season(4), _season(5))
    sonarr.remonitor_season.side_effect = SafeHTTPError(0, "timeout", "sonarr/series")

    resp = ask(viewer, sonarr, [3, 4, 5])

    assert _results(resp) == {3: "failed", 4: "failed", 5: "failed"}
    assert sonarr.searched == [(9, 3)]


@both
def test_a_season_sonarr_does_not_list_is_a_422_or_told_beside_the_others(viewer, ask, route):
    sonarr = Sonarr().track(_season(3))

    assert ask(viewer, sonarr, [7]).status_code == 422
    resp = ask(viewer, sonarr, [3, 7, -1])
    assert (resp.status_code, _results(resp)) == (
        200,
        {3: "asked", 7: "not_listed", -1: "not_listed"},
    )


def _delete_season(db, number, *, tvdb_id=TVDB):
    audit = insert_audit_log(db, media_item_id="502", action="deleted")
    db.execute(
        "INSERT INTO deleted_media (audit_id, media_type, tvdb_id, season_number, title)"
        " VALUES (?, 'tv_season', ?, ?, 'Glass Coast')",
        (audit, tvdb_id, number),
    )
    db.commit()
    return audit


@both
@pytest.mark.parametrize("tvdb_id", [TVDB, None])
def test_a_viewer_cannot_bring_back_a_season_mediaman_deleted(viewer, db, ask, route, tvdb_id):
    _delete_season(db, 2, tvdb_id=tvdb_id)
    sonarr = Sonarr().track(_season(2), _season(3))

    # The name in the request is not what the deletion is looked for by.
    more = {"title": "Another name"} if route == "search" else {}
    alone = ask(viewer, sonarr, [2], **more)
    beside = ask(viewer, sonarr, [2, 3], **more)

    assert (alone.status_code, _results(alone)) == (403, {2: "removed"})
    assert (beside.status_code, _results(beside)) == (200, {2: "removed", 3: "asked"})
    assert sonarr.searched == [(9, 3)]


@pytest.mark.parametrize("route", ["pick"])
def test_a_pick_is_marked_downloaded_once_a_season_of_it_is_and_not_when_refused(
    viewer, db, ask, route
):
    _delete_season(db, 2)
    sonarr = Sonarr().track(_season(2), _season(3))
    marks = []
    for seasons in ([2], [3]):
        ask(viewer, sonarr, seasons)
        marks.append(db.execute("SELECT downloaded_at FROM user_picks").fetchone()[0])

    assert (marks[0], marks[1] is not None) == (None, True)


@pytest.mark.parametrize("route", ["search"])
def test_an_admin_can_bring_back_a_season_mediaman_deleted_though_the_last_scan_lists_it(
    authed_client, db, ask, route
):
    _in_library(db, 2)
    _delete_season(db, 2)

    resp = ask(authed_client, Sonarr().track(_season(2)), [2])

    assert _results(resp) == {2: "asked"}


@pytest.mark.parametrize("route", ["search"])
def test_a_deleted_season_downloaded_again_since_is_anybodys_to_ask_for(viewer, db, ask, route):
    _delete_season(db, 2)
    insert_audit_log(db, media_item_id=f"tvdb:{TVDB}:s2", action="re_downloaded")

    assert _results(ask(viewer, Sonarr().track(_season(2)), [2])) == {2: "asked"}


def _in_library(db, number, key="502"):
    insert_settings(db, plex_libraries='["1"]')
    insert_media_item(
        db, id=key, media_type="tv_season", season_number=number, show_title="Glass Coast"
    )
    db.execute("UPDATE media_items SET tvdb_id = ?", (TVDB,))
    db.commit()


@pytest.mark.parametrize("route", ["search"])
@pytest.mark.parametrize("how", ["pending", "deleting", "intent"])
def test_a_season_about_to_be_deleted_is_not_added_to_even_for_an_admin(
    authed_client, db, ask, route, how
):
    _in_library(db, 2)
    if how == "intent":
        db.execute(
            "INSERT INTO delete_intents (media_item_id, target_kind, target_id, started_at)"
            " VALUES ('502', 'sonarr', '9', '2026-01-01')"
        )
        db.commit()
    else:
        insert_scheduled_action(
            db, media_item_id="502", action="scheduled_deletion", delete_status=how
        )
    sonarr = Sonarr().track(_season(2, files=3))

    resp = ask(authed_client, sonarr, [2])

    assert (resp.status_code, _results(resp)) == (409, {2: "leaving"})
    assert sonarr.searched == []


@pytest.mark.parametrize("route", ["search"])
def test_a_season_plex_holds_elsewhere_is_not_searched(viewer, db, ask, route):
    _in_library(db, 2)
    sonarr = Sonarr().track(_season(2))

    assert _results(ask(viewer, sonarr, [2])) == {2: "held"}
    assert sonarr.searched == []


@pytest.mark.parametrize("route", ["search"])
def test_a_deletion_sonarr_is_still_to_be_told_of_stands_when_another_season_is_asked_for(
    viewer, db, ask, route
):
    for number in (2, 3):
        audit = _delete_season(db, number)
        db.execute("INSERT INTO pending_unmonitors (audit_id, sonarr_id) VALUES (?, 9)", (audit,))
    db.commit()
    ask(viewer, Sonarr().track(_season(2), _season(3), _season(4)), [4])

    assert [was_requested_since(db, row) for row in fetch_pending_unmonitors(db)] == [False, False]

    insert_audit_log(db, media_item_id=f"tvdb:{TVDB}:s3", action="downloaded")
    assert [was_requested_since(db, row) for row in fetch_pending_unmonitors(db)] == [False, True]


@pytest.mark.parametrize("route", ["search"])
@pytest.mark.parametrize(("who", "asked_again"), [("viewer", False), ("authed_client", True)])
def test_only_an_admin_undoes_a_deletion_sonarr_is_still_to_be_told_of(
    request, db, ask, route, who, asked_again
):
    client = request.getfixturevalue(who)
    audit = _delete_season(db, 2)
    db.execute("INSERT INTO pending_unmonitors (audit_id, sonarr_id) VALUES (?, 9)", (audit,))
    db.commit()

    resp = ask(client, Sonarr().track(_season(2, monitored=True)), [2])

    assert _results(resp) == {2: "asked" if asked_again else "removed"}
    [row] = fetch_pending_unmonitors(db)
    assert was_requested_since(db, row) is asked_again


def _off(*seasons):
    sonarr = Sonarr().track(*seasons)
    sonarr.show["monitored"] = False
    return sonarr


@both
def test_a_viewer_cannot_start_the_other_seasons_of_a_show_that_is_off(viewer, db, ask, route):
    sonarr = _off(_season(1, monitored=True), _season(3))

    resp = ask(viewer, sonarr, [3])

    assert (resp.status_code, _results(resp)) == (409, {3: "show_unmonitored"})
    assert (sonarr.searched, _audit(db)) == ([], [])


@pytest.mark.parametrize("route", ["search"])
def test_a_viewer_may_turn_on_a_show_none_but_the_named_seasons_of_which_start(viewer, ask, route):
    sonarr = _off(_season(1), _season(3), _season(4, monitored=True))

    resp = ask(viewer, sonarr, [3, 4])

    assert _results(resp) == {3: "asked", 4: "already_asked"}


@pytest.mark.parametrize("route", ["search"])
def test_an_admin_is_told_and_audited_for_the_other_seasons_that_start(
    authed_client, db, ask, route
):
    sonarr = _off(_season(1, monitored=True), _season(3), _season(5, monitored=True))

    resp = ask(authed_client, sonarr, [3])

    assert (resp.status_code, _results(resp)) == (200, {3: "asked"})
    assert resp.json()["also_started"] == [1, 5]
    [row] = _audit(db)
    assert row[0] == f"tvdb:{TVDB}:s3" and "season 1, 5" in row[2]


@pytest.mark.parametrize("route", ["search"])
@pytest.mark.parametrize("why", ["removed", "leaving"])
def test_no_one_starts_a_deleted_or_leaving_season_by_asking_for_another(
    authed_client, db, ask, route, why
):
    if why == "removed":
        _delete_season(db, 1)
    else:
        _in_library(db, 1)
        insert_scheduled_action(db, media_item_id="502", action="scheduled_deletion")
    sonarr = _off(_season(1, monitored=True), _season(3))

    assert _results(ask(authed_client, sonarr, [3])) == {3: "show_unmonitored"}
    assert sonarr.searched == []


@pytest.mark.parametrize("route", ["search"])
@pytest.mark.parametrize(
    ("when", "status", "result"),
    [
        ("before the add", "pending", "leaving"),
        ("before the record", "deleting", "leaving"),
        # As one scheduled a moment after the season was asked for.
        ("before the record", "pending", "asked"),
    ],
)
def test_a_deletion_that_comes_while_a_season_is_asked_for_is_not_undone_by_it(
    authed_client, db, ask, route, when, status, result
):
    _in_library(db, 2)
    sonarr = Sonarr().track(_season(2, files=3))

    def schedule():
        # The route runs in a thread of its own.
        insert_scheduled_action(
            get_db(), media_item_id="502", action="scheduled_deletion", delete_status=status
        )

    def monitor(series_id, number, **how):
        if when == "before the add":
            schedule()
        sonarr._monitor(series_id, number, **how)
        schedule()

    sonarr.remonitor_season.side_effect = monitor

    resp = ask(authed_client, sonarr, [2])

    assert _results(resp) == {2: result}
    assert bool(_audit(db)) is (result == "asked")
    assert sonarr.take_back_season.called is (status == "deleting")


@pytest.mark.parametrize("route", ["search", "pick"])
def test_a_season_whose_record_fails_is_told_apart_and_the_rest_is_left_for_another_time(
    viewer, db, ask, route, monkeypatch
):
    real = _seasons.record_season

    def record(*args, number, **kwargs):
        if number == 4:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, number=number, **kwargs)

    monkeypatch.setattr(_seasons, "record_season", record)
    sonarr = Sonarr().track(_season(3), _season(4), _season(5))

    resp = ask(viewer, sonarr, [3, 4, 5])

    assert resp.status_code == 200
    # The next record would wait as long for the same lock.
    assert _results(resp) == {3: "asked", 4: "unrecorded", 5: "failed"}
    assert "Season 4 is asked for, but mediaman couldn't note it" in resp.json()["message"]
    assert sonarr.searched == [(9, 3), (9, 4)]

    # Alone it is a failure, so that the same may be sent again at once.
    assert ask(viewer, Sonarr().track(_season(4)), [4]).status_code == 502
    monkeypatch.setattr(_seasons, "record_season", real)
    again = ask(viewer, Sonarr().track(_season(4, monitored=True)), [4])
    assert _results(again) == {4: "already_asked"}


@pytest.mark.parametrize("route", ["search"])
def test_once_a_record_has_failed_nothing_more_is_recorded(
    authed_client, db, ask, route, monkeypatch
):
    waits = []

    def fails(*_args, **_kwargs):
        waits.append(1)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(_seasons, "record_season", fails)
    monkeypatch.setattr(_seasons, "record_started", fails)

    resp = ask(authed_client, _off(_season(1, monitored=True), _season(2), _season(3)), [2, 3])

    assert _results(resp) == {2: "unrecorded", 3: "failed"}
    assert resp.json()["also_started"] == [1]
    assert len(waits) == 1


@pytest.mark.parametrize("route", ["pick"])
def test_a_pick_that_cannot_be_marked_still_tells_what_came_of_its_seasons(
    viewer, db, ask, route, monkeypatch
):
    def fails(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(recommended_api.user_picks, "mark_downloaded", fails)

    resp = ask(viewer, Sonarr().track(_season(3)), [3])

    assert (resp.status_code, _results(resp)) == (200, {3: "asked"})


@pytest.mark.parametrize("route", ["search"])
def test_seasons_started_that_cannot_be_recorded_are_told_all_the_same(
    authed_client, db, ask, route, monkeypatch
):
    def fails(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(_seasons, "record_started", fails)
    sonarr = _off(_season(1, monitored=True), _season(2), _season(3))

    def monitor(series_id, number, **how):
        sonarr._monitor(series_id, number, **how)
        _delete_season(get_db(), number)

    def take_back(_series_id, _number, change):
        change.left_on = True

    sonarr.remonitor_season.side_effect = monitor
    sonarr.take_back_season.side_effect = take_back

    resp = ask(authed_client, sonarr, [2, 3])

    assert _results(resp) == {2: "removed", 3: "failed"}
    assert resp.json()["also_started"] == [1]
    assert sonarr.remonitor_season.call_count == 1


@pytest.mark.parametrize("route", ["search"])
def test_the_same_request_again_at_once_is_told_to_be_a_duplicate(viewer, db, ask, route):
    sonarr = Sonarr().track(_season(3))
    ask(viewer, sonarr, [3])

    again = ask(viewer, sonarr, [3])

    assert (again.status_code, again.json()["duplicate"]) == (429, True)


@pytest.mark.parametrize("route", ["search"])
@pytest.mark.parametrize("left_on", [True, False])
def test_seasons_left_started_for_one_that_was_not_added_are_told_and_audited(
    authed_client, db, ask, route, left_on
):
    sonarr = _off(_season(1, monitored=True), _season(2), _season(3, monitored=False))

    def monitor(series_id, number, **how):
        sonarr._monitor(series_id, number, **how)
        _delete_season(get_db(), number)

    def take_back(_series_id, _number, change):
        change.left_on = left_on

    sonarr.remonitor_season.side_effect = monitor
    sonarr.take_back_season.side_effect = take_back

    resp = ask(authed_client, sonarr, [2])

    assert _results(resp) == {2: "removed"}
    assert resp.json()["also_started"] == ([1] if left_on else [])
    assert [("season 1" in row[2], row[3]) for row in _audit(db)] == [(True, "admin")] * left_on


@pytest.mark.parametrize("route", ["search"])
def test_a_season_deleted_while_an_admin_asks_for_it_is_not_recorded_as_asked_for_again(
    authed_client, db, ask, route
):
    sonarr = Sonarr().track(_season(2))

    def monitor(series_id, number, **how):
        sonarr._monitor(series_id, number, **how)
        _delete_season(get_db(), 2)

    sonarr.remonitor_season.side_effect = monitor

    assert _results(ask(authed_client, sonarr, [2])) == {2: "removed"}
    assert _audit(db) == []


@pytest.mark.parametrize("route", ["search"])
def test_an_added_season_whose_line_was_lost_gets_it_when_asked_for_again_and_once(
    viewer, db, ask, route
):
    sonarr = Sonarr().track(_season(3, monitored=True), _season(4))

    first = ask(viewer, sonarr, [3])
    again = ask(viewer, sonarr, [3, 4])

    assert (_results(first), _results(again)) == (
        {3: "already_asked"},
        {3: "already_asked", 4: "asked"},
    )
    assert [(row[0], row[3]) for row in _audit(db)] == [
        (f"tvdb:{TVDB}:s3", "amy"),
        (f"tvdb:{TVDB}:s4", "amy"),
    ]


@pytest.mark.parametrize("route", ["search"])
def test_a_season_sonarr_gives_in_a_shape_that_cannot_be_read_fails_and_the_others_do_not(
    viewer, ask, route
):
    odd = {"seasonNumber": 3, "monitored": False, "statistics": "x"}
    sonarr = Sonarr().track(_season(2), odd, None)
    sonarr.remonitor_season.side_effect = lambda *_a, guard, **_how: guard(
        copy.deepcopy(sonarr.show)
    )

    assert _results(ask(viewer, sonarr, [2, 3, 4])) == {2: "asked", 3: "failed", 4: "failed"}


def test_a_slow_sonarr_ends_the_request_in_time(conn, fake_http, fake_response, monkeypatch):
    clock = count(0.0, 2.0)
    monkeypatch.setattr(_sonarr_methods, "monotonic", lambda: next(clock))
    create_user(conn, "boss", "password123", enforce_policy=False)
    series = {"id": 9, "monitored": True, "seasons": [_season(n) for n in range(1, 21)]}

    def arr(method, url, **_kwargs):
        if "tvdbId" in url:
            return fake_response(json_data=[series])
        return fake_response(json_data=[] if "command" in url and method == "GET" else series)

    fake_http.handler(arr)
    resp = add_to_tracked_show(
        conn,
        ArrClient(SONARR_SPEC, "http://sonarr:8989", "key"),
        tmdb_id=77,
        tvdb_id=TVDB,
        title="Glass Coast",
        numbers=range(1, 21),
        actor="boss",
        email=None,
    )

    results = [s["result"] for s in json.loads(resp.body)["seasons"]]
    assert results[0] == "asked" and results[-1] == "failed"
    assert next(clock) < REQUEST_SECONDS * 2


@pytest.mark.parametrize("route", ["search"])
def test_a_viewer_cannot_start_the_lone_monitored_episodes_of_a_season_of_a_show_that_is_off(
    viewer, db, ask, route
):
    lone = _season(1, files=3)
    lone["statistics"]["episodeCount"] = 4

    assert _results(ask(viewer, _off(lone, _season(3)), [3])) == {3: "show_unmonitored"}


def test_a_change_that_failed_though_it_landed_is_a_failure_with_no_line_and_no_mail(
    conn, fake_http, fake_response
):
    create_user(conn, "amy", "password123", enforce_policy=False, role="viewer")
    held = {"id": 9, "tvdbId": TVDB, "monitored": True, "seasons": [_season(3)]}

    def arr(method, url, **kwargs):
        if method == "PUT":
            held.update(copy.deepcopy(kwargs["json"]))
            if held["seasons"][0]["monitored"]:
                return fake_response(status=502, text="no")
        return fake_response(json_data=[held] if "tvdbId" in url else copy.deepcopy(held))

    fake_http.handler(arr)
    resp = add_to_tracked_show(
        conn,
        ArrClient(SONARR_SPEC, "http://sonarr:8989", "key"),
        tmdb_id=77,
        tvdb_id=TVDB,
        title="Glass Coast",
        numbers=[3],
        actor="amy",
        email="amy@example.com",
    )

    assert json.loads(resp.body)["seasons"] == [{"number": 3, "result": "failed"}]
    assert held["seasons"][0]["monitored"] is False
    assert (_audit(conn), _mails(conn)) == ([], [])


def test_the_history_line_names_the_show():
    row = AuditRow(
        id=1,
        media_item_id=f"tvdb:{TVDB}:s3",
        action="downloaded",
        detail="Added season 3 of 'Glass Coast' to Sonarr by amy",
        space_reclaimed_bytes=None,
        created_at="2026-01-01T00:00:00+00:00",
        mi_title=None,
        plex_rating_key=None,
        ks_title=None,
        actor="amy",
    )

    assert _build_item(row)["title"] == "Glass Coast"
