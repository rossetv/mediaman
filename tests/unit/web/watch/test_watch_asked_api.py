"""The row of what a person asked for and has not watched: their own requests only, what is
there first, what is on its way, and gone once watched, cancelled or deleted."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mediaman.core.deleted_media import record_deleted_media
from mediaman.core.time import now_utc
from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import disk_cache
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.watch_catalog import _arr, _library
from mediaman.web.auth.session_store import create_session
from mediaman.web.repository import asked, ratings
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.watch_progress import MarkTarget, mark_watched
from mediaman.web.routes.watch import asked_api
from tests.helpers.factories import (
    insert_admin_user,
    insert_audit_log,
    insert_download_notification,
    insert_media_item,
    insert_scheduled_action,
    insert_settings,
)
from tests.unit.web.watch.test_watch_catalog_api import FakeArr

NOW = now_utc().replace(microsecond=0)
ADDRESS = "aoife@example.com"


def _ago(**kw) -> datetime:
    return NOW - timedelta(**kw)


class _NoTmdb:
    """A TMDB that must not be asked."""

    token = "t"

    def __getattr__(self, name):
        raise AssertionError(f"TMDB was asked: {name}")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    catalog.invalidate()
    monkeypatch.setattr(_library, "_tvdb", {})
    monkeypatch.setattr(_library, "_held", {})
    monkeypatch.setattr(asked, "_shown", {})
    monkeypatch.setattr(asked_api, "_LIMITER", ActionRateLimiter(60, 60))
    monkeypatch.setattr(TmdbClient, "from_db", classmethod(lambda *_a, **_k: _NoTmdb()))
    yield
    catalog.invalidate()


@pytest.fixture
def radarr(monkeypatch):
    fake = FakeArr()
    monkeypatch.setattr(_arr.arr_build, "build_radarr_from_db", lambda *_a: fake)
    return fake


@pytest.fixture
def sonarr(monkeypatch):
    fake = FakeArr()
    monkeypatch.setattr(_arr.arr_build, "build_sonarr_from_db", lambda *_a: fake)
    return fake


@pytest.fixture
def client_of(app_factory, conn, radarr, sonarr):
    insert_settings(conn, plex_libraries='["1"]')
    app = app_factory(asked_api.router, conn=conn)

    def make(username: str, role: str = "viewer", email: str | None = None) -> TestClient:
        insert_admin_user(conn, username=username, role=role)
        conn.execute(
            "UPDATE admin_users SET email = ?, created_at = ? WHERE username = ?",
            (email, _ago(days=100).isoformat(), username),
        )
        conn.commit()
        if email:
            _address(conn, username, email, None, at=_ago(days=100))
        client = TestClient(app)
        client.cookies.set("session_token", create_session(conn, username))
        return client

    return make


@pytest.fixture
def aoife(client_of):
    return client_of("aoife", email=ADDRESS)


def _body(client) -> dict:
    response = client.get("/api/watch/asked")
    assert response.status_code == 200
    return response.json()


def _titles(client) -> list[str]:
    return [i["title"] for i in _body(client)["items"]]


def _film(conn, key, title, tmdb_id=None, *, added=None, library=1):
    insert_media_item(
        conn,
        id=key,
        plex_rating_key=key,
        title=title,
        plex_library_id=library,
        added_at=added or _ago(days=3),
    )
    conn.execute("UPDATE media_items SET tmdb_id = ? WHERE id = ?", (tmdb_id, key))
    conn.commit()


def _season(conn, key, show, number, title, tvdb_id, episodes=("e1", "e2")):
    insert_media_item(
        conn,
        id=key,
        plex_rating_key=key,
        title=title,
        show_title=title,
        media_type="tv_season",
        season_number=number,
        show_rating_key=show,
        plex_library_id=1,
        added_at=_ago(days=1),
    )
    keys = ",".join(f'{{"key": "{key}{e}", "number": {n + 1}}}' for n, e in enumerate(episodes))
    conn.execute(
        "UPDATE media_items SET tvdb_id = ?, episode_count = ?, episode_keys = ? WHERE id = ?",
        (tvdb_id, len(episodes), f"[{keys}]", key),
    )
    conn.commit()


def _address(conn, username, email, previous, *, at, detail=None):
    """*username* took the address *email*, as the audit log has it."""
    body = detail or {"cleared": email is None, "email": email, "previous": previous}
    conn.execute("UPDATE admin_users SET email = ? WHERE username = ?", (email, username))
    conn.execute(
        "INSERT INTO audit_log (media_item_id, action, detail, created_at, actor, actor_id) VALUES"
        " ('_security', 'sec:user.email_updated', :detail, :at, :name,"
        " (SELECT id FROM admin_users WHERE username = :name))",
        {
            "detail": f"actor={username} ip=- {json.dumps(body, separators=(',', ':'))}",
            "at": at.isoformat(),
            "name": username,
        },
    )
    conn.commit()


def _ask(conn, title, tmdb_id=None, *, by="aoife", email=ADDRESS, at=None, **fields):
    """A request as search stores it: the notification with the person's name."""
    fields.setdefault("notified", 1)
    if fields.get("tvdb_id"):
        fields |= {"media_type": "tv", "service": "sonarr"}
    insert_download_notification(
        conn,
        title=title,
        tmdb_id=tmdb_id,
        username=by,
        email=email,
        created_at=at or _ago(days=20),
        **fields,
    )


def _audit(conn, title, *, by="aoife", to="Radarr", at=None, actor_id=None):
    detail = f"Added '{title}' to {to} by {by}" if to else f"Downloaded '{title}'"
    conn.execute(
        "INSERT INTO audit_log (media_item_id, action, detail, created_at, actor, actor_id)"
        " VALUES (:title, 'downloaded', :detail, :at, :by,"
        " COALESCE(:id, (SELECT id FROM admin_users WHERE username = :by)))",
        {
            "title": title,
            "detail": detail,
            "at": (at or _ago(days=20)).isoformat(),
            "by": by,
            "id": actor_id,
        },
    )
    conn.commit()


def _delete(conn, title, tmdb_id, at, *, media_type="movie", tvdb_id=None):
    audit_id = insert_audit_log(conn, media_item_id="x", action="deleted", created_at=at)
    record_deleted_media(
        conn,
        audit_id,
        media_type=media_type,
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        season_number=None,
        title=title,
    )
    conn.commit()


def _waited(tmdb_id, title, **fields):
    return {"tmdbId": tmdb_id, "title": title, "monitored": True, "hasFile": False, **fields}


class TestWhose:
    def test_a_person_sees_what_they_asked_for_with_its_arrival(self, aoife, conn):
        _film(conn, "101", "Glass House", 11, added=_ago(days=3))
        _ask(conn, "Glass House", 11)

        (item,) = _body(aoife)["items"]

        assert (item["ref"], item["state"], item["key"]) == ("movie-101", "ready", "movie-11")
        assert datetime.fromisoformat(item["arrived_at"]) == _ago(days=3)
        assert (item["mark_kind"], item["mark_key"], item["tmdb_id"]) == ("movie", "101", 11)

    @pytest.mark.parametrize("role", ["viewer", "admin"])
    def test_another_persons_request_is_never_there(self, client_of, conn, role):
        other = client_of("cian", role=role, email="cian@example.com")
        client_of("aoife", email=ADDRESS)
        _film(conn, "101", "Glass House", 11)
        _film(conn, "102", "Named Only")
        _ask(conn, "Glass House", 11)
        _audit(conn, "Named Only")

        assert _titles(other) == []

    def test_it_says_what_they_rated_and_nothing_of_others(self, aoife, client_of, conn):
        client_of("cian")
        _ask(conn, "Salt", 10)
        _ask(conn, "Reef", 11)
        _film(conn, "100", "Salt", 10)
        _film(conn, "110", "Reef", 11)
        for name, tmdb_id, level in (("aoife", 10, 2), ("aoife", 11, 1), ("cian", 10, -2)):
            title = ratings.RatedTitle("movie", tmdb_id)
            ratings.set_level(conn, ratings.find_user_id(conn, name), title, level, NOW)

        said = {i["title"]: (i["tmdb_ref"], i["level"]) for i in _body(aoife)["items"]}

        assert said == {"Salt": ("tmdb-movie-10", 2), "Reef": ("tmdb-movie-11", 1)}

    def test_a_request_with_no_email_is_found_by_who_was_signed_in(self, aoife, conn, radarr):
        _film(conn, "101", "Glass House")
        _audit(conn, "Glass House")
        _audit(conn, "Far Shore")
        radarr.movies = [_waited(12, "Far Shore", isAvailable=True)]

        assert _titles(aoife) == ["Glass House", "Far Shore"]

    def test_a_name_two_titles_share_is_left_out(self, aoife, conn, radarr):
        _film(conn, "101", "Dune")
        _film(conn, "102", "Dune")
        radarr.movies = [_waited(n, "Far Shore", isAvailable=True) for n in (12, 13)]
        _audit(conn, "Dune")
        _audit(conn, "Far Shore")

        assert _titles(aoife) == []

    def test_an_emailed_link_is_that_of_the_account_with_the_address(self, aoife, conn):
        _film(conn, "101", "Glass House", 11)
        _ask(conn, "Glass House", 11, by=None, email="AOIFE@example.com")

        assert _titles(aoife) == ["Glass House"]

    def test_an_emailed_link_another_account_opened_is_not(self, aoife, client_of, conn):
        cian = client_of("cian")
        _film(conn, "101", "Glass House", 11)
        _ask(conn, "Glass House", 11, by=None)
        _audit(conn, "Glass House", by="cian", to=None, at=_ago(days=20) + timedelta(seconds=2))

        assert _titles(aoife) == []
        assert _titles(cian) == ["Glass House"]

    def test_a_request_from_before_the_account_is_someone_elses(self, aoife, conn):
        _film(conn, "101", "Glass House", 11)
        _film(conn, "102", "Far Shore")
        _ask(conn, "Glass House", 11, by=None, at=_ago(days=200))
        insert_audit_log(
            conn,
            media_item_id="Far Shore",
            action="downloaded",
            detail="Added 'Far Shore' to Radarr by aoife",
            actor="aoife",
            created_at=_ago(days=200),
        )

        assert _titles(aoife) == []

    def test_an_address_two_accounts_have_names_neither(self, aoife, client_of, conn):
        cian = client_of("cian", email=ADDRESS)
        _film(conn, "101", "Glass House", 11)
        _ask(conn, "Glass House", 11, by=None)

        assert (_titles(aoife), _titles(cian)) == ([], [])

    def test_an_address_changed_hands_takes_no_request_along(self, aoife, client_of, conn):
        cian = client_of("cian", email="cian@example.com")
        _film(conn, "101", "Glass House", 11)
        _film(conn, "102", "Far Shore", 12)
        _ask(conn, "Glass House", 11, by=None, at=_ago(days=20))
        _address(conn, "aoife", "new@example.com", ADDRESS, at=_ago(days=9))
        _address(conn, "cian", ADDRESS, "cian@example.com", at=_ago(days=8))
        _ask(conn, "Far Shore", 12, by=None, at=_ago(days=2))

        assert (_titles(aoife), _titles(cian)) == ([], ["Far Shore"])

    def test_an_address_saved_again_unchanged_keeps_its_requests(self, aoife, conn):
        _film(conn, "101", "Glass House", 11)
        _ask(conn, "Glass House", 11, by=None, at=_ago(days=20))
        _address(conn, "aoife", ADDRESS.upper(), ADDRESS, at=_ago(days=5))

        assert _titles(aoife) == ["Glass House"]

    def test_an_address_given_up_for_a_while_counts_from_its_return(self, aoife, conn):
        _film(conn, "101", "Glass House", 11)
        _ask(conn, "Glass House", 11, by=None, at=_ago(days=20))
        _address(conn, "aoife", None, ADDRESS, at=_ago(days=9))
        _address(conn, "aoife", ADDRESS, None, at=_ago(days=8))

        assert _titles(aoife) == []

    def test_an_admin_s_change_for_the_person_counts(self, aoife, client_of, conn):
        client_of("boss", role="admin")
        _film(conn, "101", "Glass House", 11)
        _film(conn, "102", "Far Shore", 12)
        _ask(conn, "Glass House", 11, by=None, at=_ago(days=20))
        _ask(conn, "Far Shore", 12, by=None, at=_ago(days=2))
        body = {"cleared": False, "email": ADDRESS, "previous": "old@example.com"}
        body["target_username"] = "aoife"
        _address(conn, "boss", None, None, at=_ago(days=9), detail=body)

        assert _titles(aoife) == ["Far Shore"]

    def test_an_address_of_unknown_date_brings_no_emailed_link(self, client_of, conn):
        aoife = client_of("aoife")
        _film(conn, "101", "Glass House", 11)
        _ask(conn, "Glass House", 11, by=None)
        conn.execute("UPDATE admin_users SET email = ?", (ADDRESS,))
        conn.commit()

        assert _titles(aoife) == []

    def test_an_older_entry_that_names_no_address_is_taken_for_the_change(self, aoife, conn):
        _film(conn, "101", "Glass House", 11)
        _film(conn, "102", "Far Shore", 12)
        _ask(conn, "Glass House", 11, by=None, at=_ago(days=20))
        _ask(conn, "Far Shore", 12, by=None, at=_ago(days=2))
        _address(conn, "aoife", ADDRESS, None, at=_ago(days=9), detail={"cleared": False})

        assert _titles(aoife) == ["Far Shore"]

    def test_a_history_four_times_as_long_costs_four_times_as_much(self, aoife, conn):
        def lines_run() -> int:
            """The lines of the module run to read all of it."""
            ran = 0

            def trace(frame, event, _arg):
                nonlocal ran
                if frame.f_code.co_filename != asked.__file__:
                    return None
                ran += event == "line"
                return trace

            before = sys.gettrace()
            sys.settrace(trace)
            try:
                found = asked.requests_of(conn, "aoife")
                over = asked.is_over(conn, "aoife", found)
            finally:
                sys.settrace(before)
            assert len(found) == len(over) == made
            return ran

        made = 0
        costs = []
        for upto in (20, 80):
            while made < upto:
                made += 1
                at = _ago(days=50) + timedelta(hours=made)
                # Some by emailed links, which the audit rows of everybody may be of.
                _ask(conn, f"Film {made}", made, at=at, by=None if made % 4 == 0 else "aoife")
                _audit(conn, f"Film {made}", at=at)
                _delete(conn, f"Film {made}", made, at + timedelta(minutes=1))
            costs.append(lines_run())

        assert costs[1] < 6 * costs[0]

    def test_a_removed_account_of_the_same_name_leaves_nothing_behind(self, aoife, conn):
        _film(conn, "101", "Glass House", 11)
        _audit(conn, "Glass House", actor_id=9999)

        assert _titles(aoife) == []


class TestWatched:
    def test_a_film_marked_as_watched_leaves(self, aoife, conn):
        _film(conn, "101", "Glass House", 11)
        _ask(conn, "Glass House", 11)
        mark_watched(conn, "aoife", [MarkTarget("101", "101", None, 100.0)], now=NOW)
        conn.commit()

        assert _titles(aoife) == []

    def test_someone_elses_mark_takes_nothing_away(self, aoife, client_of, conn):
        client_of("cian")
        _film(conn, "101", "Glass House", 11)
        _ask(conn, "Glass House", 11)
        mark_watched(conn, "cian", [MarkTarget("101", "101", None, 100.0)], now=NOW)
        conn.commit()

        assert _titles(aoife) == ["Glass House"]

    def test_a_show_stays_until_its_last_episode(self, aoife, conn):
        _season(conn, "501", "500", 1, "Coast", 77)
        _ask(conn, "Coast", tvdb_id=77)
        mark_watched(conn, "aoife", [MarkTarget("501e1", "501", "500", 100.0)], now=NOW)
        conn.commit()

        (item,) = _body(aoife)["items"]
        assert (item["ref"], item["subtitle"]) == ("show-500", "TV · 1 season · 1 left")

        mark_watched(conn, "aoife", [MarkTarget("501e2", "501", "500", 100.0)], now=NOW)
        conn.commit()
        assert _titles(aoife) == []

    def test_what_was_played_in_plex_itself_leaves_too(self, aoife, conn):
        _film(conn, "101", "Glass House", 11)
        _season(conn, "501", "500", 1, "Coast", 77)
        _ask(conn, "Glass House", 11)
        _ask(conn, "Coast", tvdb_id=77)
        mark_watched(conn, "aoife", [MarkTarget("501e1", "501", "500", 100.0)], now=NOW)
        conn.execute(
            "INSERT INTO plex_links (username, plex_account_id, server_account_id, plex_name,"
            " server_id, linked_at) VALUES ('aoife', 1, 1, 'a', 's', '2026-01-01')"
        )
        conn.executemany(
            "INSERT INTO plex_plays (username, history_key, media_type, title, show_rating_key,"
            " rating_key, viewed_at) VALUES ('aoife', ?, ?, 't', ?, ?, ?)",
            [
                ("1", "movie", None, "101", NOW.isoformat()),
                ("2", "episode", "500", "501e2", NOW.isoformat()),
            ],
        )
        conn.commit()

        assert _titles(aoife) == []

    def test_a_season_asked_for_again_counts_by_itself(self, aoife, conn):
        _season(conn, "501", "500", 1, "Coast", 77)
        _season(conn, "502", "500", 2, "Coast", 77)
        _ask(conn, "Coast", tvdb_id=77, season_number=1)
        mark_watched(
            conn,
            "aoife",
            [MarkTarget(f"501{e}", "501", "500", 100.0) for e in ("e1", "e2")],
            now=NOW,
        )
        conn.commit()

        assert _titles(aoife) == []

    def test_what_they_have_begun_comes_after_what_they_have_not(self, aoife, conn):
        _film(conn, "101", "Begun", 11, added=_ago(days=1))
        _film(conn, "102", "Untouched", 12, added=_ago(days=9))
        _season(conn, "501", "500", 1, "Coast", 77)
        for title, tmdb_id in (("Begun", 11), ("Untouched", 12)):
            _ask(conn, title, tmdb_id)
        _ask(conn, "Coast", tvdb_id=77)
        mark_watched(conn, "aoife", [MarkTarget("501e1", "501", "500", 100.0)], now=NOW)
        conn.execute(
            "INSERT INTO watch_progress (username, rating_key, media_item_id, position_s,"
            " duration_s, watched, updated_at) VALUES ('aoife', '101', '101', 900, 3600, 0, ?)",
            (NOW.isoformat(),),
        )
        conn.commit()

        items = _body(aoife)["items"]
        assert [(i["title"], i["started"]) for i in items] == [
            ("Untouched", False),
            ("Begun", True),
            ("Coast", True),
        ]

    def test_the_season_after_one_they_have_seen_is_not_begun(self, aoife, conn):
        _season(conn, "501", "500", 1, "Coast", 77)
        _season(conn, "502", "500", 2, "Coast", 77)
        _ask(conn, "Coast", tvdb_id=77)
        mark_watched(
            conn,
            "aoife",
            [MarkTarget(f"501{e}", "501", "500", 100.0) for e in ("e1", "e2")],
            now=NOW,
        )
        conn.commit()

        assert [i["started"] for i in _body(aoife)["items"]] == [False]

    def test_the_season_asked_for_is_new_though_another_is_half_seen(self, aoife, conn):
        _season(conn, "501", "500", 1, "Coast", 77)
        _season(conn, "502", "500", 2, "Coast", 77)
        _ask(conn, "Coast", tvdb_id=77, season_number=2)
        mark_watched(conn, "aoife", [MarkTarget("501e1", "501", "500", 100.0)], now=NOW)
        conn.commit()
        assert [i["started"] for i in _body(aoife)["items"]] == [False]

        mark_watched(conn, "aoife", [MarkTarget("502e1", "502", "500", 100.0)], now=NOW)
        conn.commit()
        assert [i["started"] for i in _body(aoife)["items"]] == [True]


class TestOnItsWay:
    @pytest.mark.parametrize(
        ("waits_for", "day"),
        [("released", 100), ("inCinemas", 10), ("announced", 10), (None, 10)],
    )
    def test_a_film_comes_on_the_date_radarr_waits_for(self, aoife, conn, radarr, waits_for, day):
        dates = {
            key: (NOW + timedelta(days=days)).isoformat()
            for key, days in (("inCinemas", 10), ("digitalRelease", 100), ("physicalRelease", 130))
        }
        radarr.movies = [
            _waited(11, "Glass House", isAvailable=False, minimumAvailability=waits_for, **dates)
        ]
        _ask(conn, "Glass House", 11)

        (item,) = _body(aoife)["items"]
        assert item["release_date"] == (NOW + timedelta(days=day)).date().isoformat()

    def test_a_film_with_no_date_but_the_cinemas_has_none(self, aoife, conn, radarr):
        radarr.movies = [
            _waited(
                11,
                "Glass House",
                isAvailable=False,
                minimumAvailability="released",
                inCinemas=(NOW + timedelta(days=10)).isoformat(),
            )
        ]
        _ask(conn, "Glass House", 11)

        (item,) = _body(aoife)["items"]
        assert (item["state"], item["release_date"]) == ("coming", None)

    def test_what_is_there_comes_first_then_downloads_then_what_is_not_out(
        self, aoife, conn, radarr
    ):
        _film(conn, "101", "Older", 11, added=_ago(days=9))
        _film(conn, "102", "Newer", 12, added=_ago(hours=2))
        later, soon = NOW + timedelta(days=90), NOW + timedelta(days=30)
        radarr.movies = [
            _waited(21, "Undated", isAvailable=False),
            _waited(22, "Later", isAvailable=False, digitalRelease=later.isoformat()),
            _waited(23, "Soon", isAvailable=False, digitalRelease=soon.isoformat()),
            _waited(24, "Coming Down", isAvailable=True),
            _waited(25, "Searched", isAvailable=True),
        ]
        radarr.queue = [{"movie": {"tmdbId": 24}, "size": 100, "sizeleft": 60, "status": "x"}]
        for tmdb_id, title in (
            *((11, "Older"), (12, "Newer"), (21, "Undated"), (22, "Later")),
            *((23, "Soon"), (24, "Coming Down"), (25, "Searched")),
        ):
            _ask(conn, title, tmdb_id)

        items = _body(aoife)["items"]

        assert [(i["title"], i["state"]) for i in items] == [
            ("Newer", "ready"),
            ("Older", "ready"),
            ("Coming Down", "downloading"),
            ("Searched", "queued"),
            ("Soon", "coming"),
            ("Later", "coming"),
            ("Undated", "coming"),
        ]
        dates = {i["title"]: i["release_date"] for i in items}
        assert (dates["Soon"], dates["Undated"]) == (soon.date().isoformat(), None)
        assert items[2]["download_pct"] == 40

    def test_a_show_that_has_not_aired_says_when_it_does(self, aoife, conn, sonarr):
        airs = (NOW + timedelta(days=12)).isoformat()
        sonarr.series = [
            {
                "tmdbId": 70,
                "tvdbId": 77,
                "title": "Coast",
                "monitored": True,
                "seasons": [
                    {"seasonNumber": 1, "monitored": True, "statistics": {"nextAiring": airs}}
                ],
            }
        ]
        _ask(conn, "Coast", tvdb_id=77)

        (item,) = _body(aoife)["items"]

        assert (item["ref"], item["state"]) == ("tmdb-tv-70", "coming")
        assert item["release_date"] == airs[:10]

    @pytest.mark.parametrize(("hours", "shown"), [(1, ["Glass House"]), (72, [])])
    @pytest.mark.parametrize("notified", [0, 1])
    def test_a_file_plex_does_not_list_yet_is_on_its_way_for_a_while(
        self, aoife, conn, radarr, hours, shown, notified
    ):
        added = _ago(hours=hours).isoformat()
        radarr.movies = [
            _waited(11, "Glass House", hasFile=True, movieFile={"dateAdded": added}),
        ]
        _ask(conn, "Glass House", 11, notified=notified)

        items = _body(aoife)["items"]
        assert [i["title"] for i in items] == shown
        assert all(i["state"] == "arriving" for i in items)

    @pytest.mark.parametrize(
        ("asked_for", "aired", "states"),
        [(5, 400, ["arriving"]), (60, 3, ["arriving"]), (60, 40, [])],
    )
    def test_a_show_plex_does_not_list_yet_is_on_its_way_for_a_while(
        self, aoife, conn, sonarr, asked_for, aired, states
    ):
        statistics = {
            "episodeCount": 8,
            "episodeFileCount": 8,
            "totalEpisodeCount": 8,
            "previousAiring": _ago(days=aired).isoformat(),
        }
        season = {"seasonNumber": 1, "monitored": True, "statistics": statistics}
        series = {"tmdbId": 70, "tvdbId": 77, "title": "Coast", "monitored": True}
        sonarr.series = [
            {
                **series,
                "seasons": [season],
                "statistics": {"episodeCount": 8, "episodeFileCount": 8},
            }
        ]
        _ask(conn, "Coast", tvdb_id=77, season_number=1, notified=0, at=_ago(days=asked_for))

        assert [i["state"] for i in _body(aoife)["items"]] == states

    def test_what_nothing_fetches_any_more_is_gone(self, aoife, conn, radarr):
        radarr.movies = [_waited(12, "Given Up", monitored=False)]
        _ask(conn, "Cancelled", 11)
        _ask(conn, "Given Up", 12)

        assert _titles(aoife) == []

    def test_what_is_outside_the_scanned_libraries_is_not_named(self, aoife, conn):
        _film(conn, "901", "Hidden", 11, library=9)
        _ask(conn, "Hidden", 11)

        assert _titles(aoife) == []

    def test_a_service_that_does_not_answer_is_said(self, aoife, conn, radarr):
        _film(conn, "101", "Glass House", 11)
        _ask(conn, "Glass House", 11)
        _ask(conn, "Far Shore", 12)
        _ask(conn, "Coast", 70, tvdb_id=77)
        radarr.down = True

        body = _body(aoife)

        assert [i["title"] for i in body["items"]] == ["Glass House"]
        assert body["complete"] is False

    def test_a_poster_comes_from_radarr_else_from_what_tmdb_answered_before(
        self, aoife, conn, radarr, monkeypatch
    ):
        held = {"tmdb:details:v5:movie:22": {"poster_url": "/api/watch/img/tmdb/w500/b.jpg"}}

        class Cache:
            def get_or_fetch(self, key, *_a, allow_fetch, **_k):
                if key in held:
                    return held[key]
                assert not allow_fetch()
                raise disk_cache.Refused(key)

        monkeypatch.setattr(disk_cache, "meta", Cache)
        remote = "https://image.tmdb.org/t/p/original/a.jpg"
        radarr.movies = [
            _waited(
                21, "A", isAvailable=True, images=[{"coverType": "poster", "remoteUrl": remote}]
            ),
            _waited(22, "B", isAvailable=True),
            _waited(23, "C", isAvailable=True),
        ]
        for tmdb_id, title in ((21, "A"), (22, "B"), (23, "C")):
            _ask(conn, title, tmdb_id)

        assert [i["poster_url"] for i in _body(aoife)["items"]] == [
            "/api/watch/img/tmdb/w342/a.jpg",
            "/api/watch/img/tmdb/w500/b.jpg",
            None,
        ]


class TestOverAndDone:
    def test_a_deleted_title_someone_else_asks_for_again_is_not_theirs(self, aoife, conn, radarr):
        _ask(conn, "Glass House", 11, at=_ago(days=20))
        _delete(conn, "Glass House", 11, _ago(days=5))
        radarr.movies = [_waited(11, "Glass House", isAvailable=True)]

        assert _titles(aoife) == []

    def test_asked_for_again_after_it_was_deleted_it_is_back(self, aoife, conn, radarr):
        _ask(conn, "Glass House", 11, at=_ago(days=20))
        _delete(conn, "Glass House", 11, _ago(days=5))
        _ask(conn, "Glass House", 11, at=_ago(days=1))
        radarr.movies = [_waited(11, "Glass House", isAvailable=True)]

        assert _titles(aoife) == ["Glass House"]

    def test_played_and_then_deleted_it_does_not_come_back(self, aoife, conn, radarr):
        _ask(conn, "Glass House", 11, at=_ago(days=20))
        conn.execute(
            "INSERT INTO played_titles VALUES ('aoife', 'movie', 'Glass House', 11, ?)",
            (_ago(days=10).isoformat(),),
        )
        conn.commit()
        radarr.movies = [_waited(11, "Glass House", isAvailable=True)]

        assert _titles(aoife) == []

    def test_a_name_whose_title_was_deleted_is_not_taken_for_another(self, aoife, conn):
        _audit(conn, "Dune", at=_ago(days=20))
        _delete(conn, "Dune", 11, _ago(days=5))
        _film(conn, "102", "Dune")

        assert _titles(aoife) == []

    def test_one_that_is_leaving_stays_and_says_so(self, aoife, conn):
        _film(conn, "101", "Glass House", 11)
        _ask(conn, "Glass House", 11)
        insert_scheduled_action(
            conn, media_item_id="101", execute_at=(NOW + timedelta(days=4)).isoformat()
        )

        (item,) = _body(aoife)["items"]
        assert item["leaving_days"] is not None and item["keep_id"] == "101"

    def test_one_gone_from_the_library_a_moment_ago_is_no_error(self, aoife, conn):
        _film(conn, "101", "Glass House", 11)
        _ask(conn, "Glass House", 11)
        assert _titles(aoife) == ["Glass House"]

        conn.execute("DELETE FROM media_items")
        conn.commit()
        assert _titles(aoife) == []


class TestOnce:
    def test_a_title_asked_for_again_and_again_is_shown_once(self, aoife, conn, radarr):
        _film(conn, "101", "Glass House", 11)
        _ask(conn, "Glass House", 11, at=_ago(days=30))
        _ask(conn, "Glass House", 11, at=_ago(days=20))
        _audit(conn, "Glass House", at=_ago(days=10))
        radarr.movies = [_waited(11, "Glass House", hasFile=True)]

        assert _titles(aoife) == ["Glass House"]

    def test_the_audit_row_of_a_request_is_not_a_second_one(self, aoife, conn, radarr):
        _ask(conn, "Glass House", 11, at=_ago(days=20))
        _audit(conn, "Glass House", at=_ago(days=20))
        radarr.movies = [
            _waited(11, "Glass House", isAvailable=True),
            _waited(12, "Glass House", isAvailable=True),
        ]

        assert [i["key"] for i in _body(aoife)["items"]] == ["movie-11"]

    def test_a_show_there_with_another_season_on_its_way_is_one_card(self, aoife, conn, sonarr):
        _season(conn, "501", "500", 1, "Coast", 77)
        _ask(conn, "Coast", tvdb_id=77, season_number=1)
        _ask(conn, "Coast", tvdb_id=77, season_number=2)
        seasons = [{"seasonNumber": 2, "monitored": True, "statistics": {"episodeCount": 8}}]
        series = {"tmdbId": 70, "tvdbId": 77, "title": "Coast", "monitored": True}
        sonarr.series = [{**series, "seasons": seasons}]

        (item,) = _body(aoife)["items"]
        assert (item["ref"], item["held_ref"], item["state"]) == ("show-500", "show-500", "ready")

        mark_watched(
            conn,
            "aoife",
            [MarkTarget(f"501{e}", "501", "500", 100.0) for e in ("e1", "e2")],
            now=NOW,
        )
        conn.commit()
        (item,) = _body(aoife)["items"]
        # The season on its way is of a show the library has.
        assert (item["ref"], item["held_ref"]) == ("tmdb-tv-70", "show-500")
        assert item["state"] == "queued"

    def test_nothing_asked_for_is_an_empty_row(self, aoife):
        assert _body(aoife) == {"ok": True, "items": [], "complete": True}

    def test_asking_too_often_is_refused(self, aoife, monkeypatch):
        monkeypatch.setattr(asked_api, "_LIMITER", ActionRateLimiter(1, 60))

        assert aoife.get("/api/watch/asked").status_code == 200
        assert aoife.get("/api/watch/asked").status_code == 429


class TestHasAny:
    @pytest.fixture
    def any_of(self, aoife, conn):
        def ask(*continuing: str) -> bool:
            catalog.clear_library_index()
            index = catalog.library_index(conn, None, ["1"], lib.library_movies, lib.library_shows)
            return asked.has_any(conn, "aoife", ["1"], index, continuing)

        return ask

    def test_nothing_asked_for_has_none(self, any_of):
        assert any_of() is False

    def test_one_unwatched_has_and_once_watched_has_not(self, any_of, conn):
        _film(conn, "101", "Glass House", 11)
        _ask(conn, "Glass House", 11)
        assert any_of() is True

        mark_watched(conn, "aoife", [MarkTarget("101", "101", None, 100.0)], now=NOW)
        assert any_of() is False

    def test_one_not_there_counts_while_its_email_is_still_to_come(self, any_of, conn):
        _ask(conn, "Far Shore", 12, notified=1)
        assert any_of() is False

        _ask(conn, "Glass House", 11, notified=0)
        assert any_of() is True

    def test_one_waited_for_too_long_holds_no_place(self, any_of, conn):
        _ask(conn, "Glass House", 11, notified=0, at=_ago(days=31))
        assert any_of() is False

    def test_one_deleted_since_holds_no_place(self, any_of, conn):
        _ask(conn, "Glass House", 11, notified=0)
        _delete(conn, "Glass House", 11, _ago(days=2))
        assert any_of() is False

    def test_a_name_whose_title_was_deleted_holds_no_place(self, any_of, conn):
        _film(conn, "101", "Glass House")
        _audit(conn, "Glass House")
        assert any_of() is True

        _delete(conn, "Glass House", None, _ago(days=2))
        assert any_of() is False

    def test_one_that_was_not_in_the_row_when_it_was_loaded_holds_no_place(
        self, any_of, aoife, conn, radarr
    ):
        _ask(conn, "Cancelled", 11, notified=0)
        assert any_of() is True

        assert _titles(aoife) == []
        assert any_of() is False

    def test_one_with_no_email_to_come_holds_its_place_once_it_was_shown(
        self, any_of, aoife, conn, radarr
    ):
        _audit(conn, "Far Shore")
        radarr.movies = [_waited(12, "Far Shore", isAvailable=True)]
        assert any_of() is False

        assert _titles(aoife) == ["Far Shore"]
        assert any_of() is True

    def test_a_service_that_does_not_answer_takes_no_place_away(
        self, any_of, aoife, conn, monkeypatch
    ):
        monkeypatch.setattr(_arr.arr_build, "build_radarr_from_db", lambda *_a: None)
        _ask(conn, "Far Shore", 12, notified=0)

        assert _titles(aoife) == []
        assert any_of() is True

    def test_a_coming_season_of_a_deleted_show_holds_its_place_once_it_was_shown(
        self, any_of, aoife, conn, sonarr
    ):
        airs = (NOW + timedelta(days=12)).isoformat()
        season = {"seasonNumber": 2, "monitored": True, "statistics": {"nextAiring": airs}}
        show = {"tmdbId": 70, "tvdbId": 77, "title": "Coast", "monitored": True}
        sonarr.series = [{**show, "seasons": [season]}]
        _ask(conn, "Coast", tvdb_id=77, notified=0)
        _delete(conn, "Coast", None, _ago(days=2), media_type="tv", tvdb_id=77)

        assert _titles(aoife) == ["Coast"]
        assert any_of() is True

    def test_a_film_shown_as_coming_and_deleted_since_holds_no_place(
        self, any_of, aoife, conn, radarr
    ):
        radarr.movies = [_waited(12, "Far Shore")]
        _ask(conn, "Far Shore", 12)
        assert _titles(aoife) == ["Far Shore"]

        _delete(conn, "Far Shore", 12, _ago(days=2))
        assert any_of() is False

    def test_one_whose_email_has_just_gone_out_keeps_its_place(self, any_of, aoife, conn, radarr):
        radarr.movies = [
            _waited(11, "Glass House", hasFile=True, movieFile={"dateAdded": NOW.isoformat()})
        ]
        _ask(conn, "Glass House", 11, notified=0)
        assert _titles(aoife) == ["Glass House"]

        conn.execute("UPDATE download_notifications SET notified = 1")
        conn.commit()
        assert any_of() is True

    def test_only_what_was_just_asked_for_and_whose_account_is_there_is_remembered(
        self, aoife, client_of, conn, radarr
    ):
        other = client_of("cian", email="cian@example.com")
        radarr.movies = [_waited(11, "Glass House"), _waited(12, "Far Shore")]
        _ask(conn, "Glass House", 11)
        _ask(conn, "Far Shore", 12)
        _ask(conn, "Far Shore", 12, by="cian", email="cian@example.com")
        assert len(_titles(aoife)) == 2
        assert len(_titles(other)) == 1

        conn.execute("DELETE FROM download_notifications WHERE tmdb_id = 12 AND username = 'aoife'")
        conn.execute("DELETE FROM admin_users WHERE username = 'cian'")
        conn.commit()
        assert _titles(aoife) == ["Glass House"]
        assert {u: [r.title for r in asked._shown[u]] for u in asked._shown} == {
            "aoife": ["Glass House"]
        }

        conn.execute("DELETE FROM download_notifications")
        conn.commit()
        assert _titles(aoife) == []
        assert asked._shown["aoife"] == {}
