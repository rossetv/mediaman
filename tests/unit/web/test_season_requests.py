"""Asking for more seasons of a show Sonarr already tracks, from search and from a pick."""

# ruff: noqa: F811

from __future__ import annotations

import copy
from unittest.mock import MagicMock, patch

import pytest

from mediaman.scanner.repository.audit import AuditRow
from mediaman.scanner.repository.scheduled_actions import (
    fetch_pending_unmonitors,
    was_requested_since,
)
from mediaman.services.infra import SafeHTTPError
from mediaman.web.auth.password_hash import create_user, set_user_email
from mediaman.web.routes.history import _build_item
from mediaman.web.routes.recommended.api import reset_download_action_limiter
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

    def _monitor(self, _series_id, number):
        next(s for s in self.show["seasons"] if s["seasonNumber"] == number)["monitored"] = True

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

    def monitor(_series_id, number):
        if number == 3:
            raise SafeHTTPError(400, "no", "sonarr/series")
        sonarr._monitor(_series_id, number)

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
    sonarr.get_series_by_id.side_effect = SafeHTTPError(0, "timeout", "sonarr/series")

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
