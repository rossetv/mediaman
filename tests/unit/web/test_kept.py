"""Tests for the kept/protected media API routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from mediaman.web.routes.kept import router as kept_router
from tests.helpers.factories import insert_kept_show, insert_media_item, insert_scheduled_action


def _insert_protection(conn, media_item_id: str, action: str = "protected_forever") -> None:
    # Snoozed/protected rows omit ``execute_at`` (the dashboard schedules-queue
    # ignores them); use the shared factory but null the execute_at default.
    insert_scheduled_action(
        conn,
        media_item_id=media_item_id,
        action=action,
        execute_at=None,
        token=f"tok-{media_item_id}",
    )


def _insert_season(
    conn, media_id: str, show_rating_key: str | None, show_title: str, season: int = 1
) -> None:
    """Insert a TV season with a specific show_rating_key / show_title."""
    insert_media_item(
        conn,
        id=media_id,
        title=f"{show_title} S{season}",
        media_type="tv_season",
        plex_rating_key=f"rk-{media_id}",
        file_path="/p",
        file_size_bytes=1,
        show_rating_key=show_rating_key,
        show_title=show_title,
        season_number=season,
    )


class TestApiKept:
    def test_requires_auth(self, app_factory, conn):
        app = app_factory(kept_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/kept")
        assert resp.status_code == 401

    def test_returns_empty(self, app_factory, authed_client, conn):
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/api/kept")
        assert resp.status_code == 200
        body = resp.json()
        assert "forever" in body
        assert "snoozed" in body
        assert body["forever"] == []
        assert body["snoozed"] == []

    def test_returns_protected_items(self, app_factory, authed_client, conn):
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        insert_media_item(
            conn, id="m1", title="Inception", plex_rating_key="rk1", file_size_bytes=0
        )
        _insert_protection(conn, "m1", "protected_forever")
        resp = client.get("/api/kept")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["forever"]) == 1
        assert body["forever"][0]["title"] == "Inception"


class TestApiUnprotect:
    def test_requires_auth(self, app_factory, conn):
        app = app_factory(kept_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/media/m1/unprotect")
        assert resp.status_code == 401

    def test_unprotect_not_found_returns_404(self, app_factory, authed_client, conn):
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.post("/api/media/m1/unprotect")
        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"

    def test_unprotect_happy_path(self, app_factory, authed_client, conn):
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        insert_media_item(conn, id="m1", title="Dune", plex_rating_key="rk1", file_size_bytes=0)
        _insert_protection(conn, "m1", "protected_forever")
        resp = client.post("/api/media/m1/unprotect")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        remaining = conn.execute(
            "SELECT COUNT(*) FROM scheduled_actions WHERE media_item_id='m1' "
            "AND action='protected_forever'"
        ).fetchone()[0]
        assert remaining == 0
        audit = conn.execute("SELECT action FROM audit_log WHERE media_item_id='m1'").fetchone()
        assert audit is not None
        assert audit["action"] == "unprotected"


class TestApiUnprotectEveryKeep:
    def test_one_press_removes_every_keep_and_writes_one_audit_row(
        self, app_factory, authed_client, conn
    ):
        client = authed_client(app_factory(kept_router, conn=conn), conn)
        insert_media_item(conn, id="m1", title="Dune", plex_rating_key="rk1", file_size_bytes=0)
        for n, action in enumerate(("snoozed", "protected_forever")):
            insert_scheduled_action(
                conn, media_item_id="m1", action=action, execute_at=None, token=f"t{n}"
            )
        scheduled = insert_scheduled_action(
            conn, media_item_id="m1", action="scheduled_deletion", token="t-sched"
        )
        conn.commit()

        assert client.post("/api/media/m1/unprotect").status_code == 200

        left = conn.execute("SELECT id, action FROM scheduled_actions").fetchall()
        assert [(r["id"], r["action"]) for r in left] == [(scheduled, "scheduled_deletion")]
        audits = conn.execute("SELECT detail FROM audit_log WHERE action='unprotected'").fetchall()
        assert len(audits) == 1
        assert "2 keeps" in audits[0]["detail"]
        assert client.post("/api/media/m1/unprotect").status_code == 404


class TestApiShowSeasons:
    def test_requires_auth(self, app_factory, conn):
        app = app_factory(kept_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/show/rk_show/seasons")
        assert resp.status_code == 401

    def test_empty_returns_no_seasons(self, app_factory, authed_client, conn):
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/api/show/rk_nonexistent/seasons")
        assert resp.status_code == 200
        body = resp.json()
        assert body["seasons"] == []
        assert body["show_title"] == ""


class TestShowSeasonsKeptByScannerRule:
    """The per-season list reads keeps by the rule the scanner applies."""

    def _seasons(self, app_factory, authed_client, conn):
        client = authed_client(app_factory(kept_router, conn=conn), conn)
        body = client.get("/api/show/rk_show/seasons").json()
        return {s["season_number"]: s["kept"] for s in body["seasons"]}, body

    def test_email_made_keeps_count_as_kept_and_lapsed_snooze_does_not(
        self, app_factory, authed_client, conn
    ):
        future = (datetime.now(UTC) + timedelta(days=5)).isoformat()
        past = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        for n in (1, 2, 3, 4):
            _insert_season(conn, f"s{n}", "rk_show", "Show", season=n)
        insert_scheduled_action(
            conn,
            media_item_id="s1",
            action="protected_forever",
            execute_at=None,
            token="t1",
            token_used=1,
        )
        insert_scheduled_action(
            conn,
            media_item_id="s2",
            action="snoozed",
            execute_at=future,
            token="t2",
            token_used=1,
        )
        insert_scheduled_action(
            conn,
            media_item_id="s3",
            action="snoozed",
            execute_at=past,
            token="t3",
            token_used=0,
        )
        conn.commit()

        kept, _ = self._seasons(app_factory, authed_client, conn)

        assert kept == {1: True, 2: True, 3: False, 4: False}

    def test_lapsed_show_snooze_is_not_reported_as_show_kept(
        self, app_factory, authed_client, conn
    ):
        _insert_season(conn, "s1", "rk_show", "Show")
        insert_kept_show(
            conn,
            show_rating_key="rk_show",
            action="snoozed",
            execute_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
        )
        conn.commit()

        _, body = self._seasons(app_factory, authed_client, conn)

        assert body["show_kept"] is None

    def test_unprotect_removes_an_email_made_keep(self, app_factory, authed_client, conn):
        _insert_season(conn, "s1", "rk_show", "Show")
        insert_scheduled_action(
            conn,
            media_item_id="s1",
            action="protected_forever",
            execute_at=None,
            token="t1",
            token_used=1,
        )
        conn.commit()
        client = authed_client(app_factory(kept_router, conn=conn), conn)

        assert client.post("/api/media/s1/unprotect").status_code == 200

        body = client.get("/api/show/rk_show/seasons").json()
        assert [x["kept"] for x in body["seasons"]] == [False]


class TestKeepShowIdorDefence:
    """/api/show/{key}/keep must not collide two shows sharing a title."""

    def test_seasons_owned_by_different_show_rejected(self, app_factory, authed_client, conn):
        """A season_id from a different show with the same title is refused."""
        # Two distinct shows, both titled "Kingdom", different rating keys.
        _insert_season(conn, "m-A", "rk-show-A", "Kingdom", season=1)
        _insert_season(conn, "m-B", "rk-show-B", "Kingdom", season=1)

        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)

        # Try to keep show A but pass show B's season id — must be rejected.
        resp = client.post(
            "/api/show/rk-show-A/keep",
            json={"duration": "forever", "season_ids": ["m-B"]},
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False
        # No protection row was created for m-B
        row = conn.execute("SELECT 1 FROM scheduled_actions WHERE media_item_id='m-B'").fetchone()
        assert row is None

    def test_unknown_rating_key_returns_409(self, app_factory, authed_client, conn):
        """A rating_key with no matching media_items row is refused with 409."""
        _insert_season(conn, "m-A", "rk-show-A", "Kingdom", season=1)

        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)

        resp = client.post(
            "/api/show/rk-does-not-exist/keep",
            json={"duration": "forever", "season_ids": ["m-A"]},
        )
        assert resp.status_code == 409
        assert resp.json()["ok"] is False

    def test_correct_ownership_still_allowed(self, app_factory, authed_client, conn):
        """Happy path — seasons with matching show_rating_key are accepted."""
        _insert_season(conn, "m-A1", "rk-show-A", "Kingdom", season=1)
        _insert_season(conn, "m-A2", "rk-show-A", "Kingdom", season=2)

        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)

        resp = client.post(
            "/api/show/rk-show-A/keep",
            json={"duration": "forever", "season_ids": ["m-A1", "m-A2"]},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM scheduled_actions WHERE media_item_id IN ('m-A1','m-A2')"
        ).fetchone()
        assert rows["n"] == 2

    def test_keep_while_a_season_is_being_deleted_keeps_every_season_and_says_so(
        self, app_factory, authed_client, conn
    ):
        for number in (1, 2, 3):
            _insert_season(conn, f"m-A{number}", "rk-show-A", "Kingdom", season=number)
        for media_id, status in (("m-A1", "pending"), ("m-A2", "deleting")):
            insert_scheduled_action(
                conn,
                media_item_id=media_id,
                action="scheduled_deletion",
                token=f"tok-{media_id}",
                token_used=False,
                delete_status=status,
            )
        client = authed_client(app_factory(kept_router, conn=conn), conn)

        resp = client.post(
            "/api/show/rk-show-A/keep",
            json={"duration": "forever", "season_ids": ["m-A1", "m-A2", "m-A3"]},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deletion_running"] is True
        assert "already being deleted" in body["message"]
        rows = conn.execute(
            "SELECT media_item_id, action, delete_status FROM scheduled_actions "
            "ORDER BY media_item_id"
        ).fetchall()
        assert [tuple(r)[:2] for r in rows] == [
            ("m-A1", "protected_forever"),
            ("m-A2", "protected_forever"),
            ("m-A3", "protected_forever"),
        ]
        assert rows[1]["delete_status"] == "deleting"
        assert conn.execute("SELECT 1 FROM kept_shows WHERE show_rating_key='rk-show-A'").fetchone()


class TestResolveShowRatingKey:
    """Helper unit tests."""

    def test_empty_supplied_key_is_refused(self, conn):
        from mediaman.web.routes.kept import _resolve_show_rating_key

        resolved, err = _resolve_show_rating_key(conn, "")
        assert resolved is None
        assert err is not None

    def test_known_key_resolves(self, conn):
        from mediaman.web.routes.kept import _resolve_show_rating_key

        _insert_season(conn, "m-A", "rk-known", "Show", season=1)
        resolved, err = _resolve_show_rating_key(conn, "rk-known")
        assert resolved == "rk-known"
        assert err is None

    def test_unknown_key_returns_error(self, conn):
        from mediaman.web.routes.kept import _resolve_show_rating_key

        resolved, err = _resolve_show_rating_key(conn, "rk-unknown")
        assert resolved is None
        assert err is not None


class TestKeptPageRedirects:
    """Auth-gated /kept and /kept/page redirect behaviour."""

    def test_kept_unauthenticated_redirects_to_login(self, app_factory, conn):
        app = app_factory(kept_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/kept", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].endswith("/login")

    def test_kept_authenticated_redirects_to_the_watch_kept_list(
        self, app_factory, authed_client, conn
    ):
        """GET /kept with a valid session 301-redirects to /watch?view=list&type=kept."""
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/kept", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers["location"] == "/watch?view=list&type=kept"

    def test_kept_page_unauthenticated_redirects_to_login(self, app_factory, conn):
        app = app_factory(kept_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/kept/page", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].endswith("/login")

    def test_kept_page_authenticated_redirects_to_the_watch_kept_list(
        self, app_factory, authed_client, conn
    ):
        client = authed_client(app_factory(kept_router, conn=conn), conn)
        resp = client.get("/kept/page", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers["location"] == "/watch?view=list&type=kept"

    def test_protected_redirect_unauthenticated(self, app_factory, conn):
        """GET /protected without a session redirects to /login."""
        app = app_factory(kept_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/protected", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].endswith("/login")

    def test_protected_redirect_authenticated(self, app_factory, authed_client, conn):
        """GET /protected with a valid session 301-redirects to /watch?view=list&type=kept."""
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/protected", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers["location"] == "/watch?view=list&type=kept"


class TestApiRemoveShowKeep:
    """POST /api/show/{key}/remove — remove a show-level keep rule."""

    def test_remove_show_keep_requires_auth(self, app_factory, conn):
        """Removing a show keep without a session returns 401."""
        app = app_factory(kept_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/show/rk-show/remove")
        assert resp.status_code == 401

    def test_remove_nonexistent_show_keep_returns_404(self, app_factory, authed_client, conn):
        """Removing a keep rule that does not exist returns 404."""
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.post("/api/show/rk-does-not-exist/remove")
        assert resp.status_code == 404

    def test_remove_show_keep_happy_path(self, app_factory, authed_client, conn):
        """Happy path: kept_shows row is deleted and ok=True returned."""
        _insert_season(conn, "m-X", "rk-show-X", "Galaxy Quest", season=1)

        # Create a keep rule first
        insert_kept_show(
            conn, show_rating_key="rk-show-X", show_title="Galaxy Quest", action="protected_forever"
        )

        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.post("/api/show/rk-show-X/remove")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        row = conn.execute("SELECT id FROM kept_shows WHERE show_rating_key='rk-show-X'").fetchone()
        assert row is None


class TestKeepShowSnoozed:
    """POST /api/show/{key}/keep with a finite duration stores a snoozed action."""

    def test_keep_show_with_duration_stores_snoozed(self, app_factory, authed_client, conn):
        """Keep with a finite duration inserts snoozed rows, not protected_forever."""
        _insert_season(conn, "m-Z", "rk-show-Z", "Firefly", season=1)

        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)

        resp = client.post(
            "/api/show/rk-show-Z/keep",
            json={"duration": "30 days", "season_ids": ["m-Z"]},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        sa = conn.execute(
            "SELECT action, snooze_duration FROM scheduled_actions WHERE media_item_id='m-Z'"
        ).fetchone()
        assert sa is not None
        assert sa["action"] == "snoozed"
        assert sa["snooze_duration"] == "30 days"

    def test_keep_show_reads_the_seasons_rows_inside_its_transaction(
        self, app_factory, authed_client, conn, monkeypatch
    ):
        """A per-season keep written between the read and the write would survive beside it."""
        from mediaman.web.routes import kept_show

        _insert_season(conn, "m-T", "rk-show-T", "Fringe", season=1)
        in_transaction = []
        real_fetch = kept_show.fetch_existing_actions_for_seasons

        def spy(spy_conn, season_ids):
            in_transaction.append(spy_conn.in_transaction)
            return real_fetch(spy_conn, season_ids)

        monkeypatch.setattr(kept_show, "fetch_existing_actions_for_seasons", spy)
        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)

        resp = client.post(
            "/api/show/rk-show-T/keep", json={"duration": "30 days", "season_ids": ["m-T"]}
        )

        assert resp.status_code == 200
        assert in_transaction == [True]

    def test_keep_show_invalid_duration_returns_400(self, app_factory, authed_client, conn):
        _insert_season(conn, "m-Y", "rk-show-Y", "Futurama", season=1)

        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)

        resp = client.post(
            "/api/show/rk-show-Y/keep",
            json={"duration": "2 years", "season_ids": ["m-Y"]},
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_keep_show_no_seasons_returns_400(self, app_factory, authed_client, conn):
        """Submitting an empty season_ids list returns 400."""
        _insert_season(conn, "m-Q", "rk-show-Q", "The Wire", season=1)

        app = app_factory(kept_router, conn=conn)
        client = authed_client(app, conn)

        resp = client.post(
            "/api/show/rk-show-Q/keep",
            json={"duration": "forever", "season_ids": []},
        )
        assert resp.status_code == 400
        assert resp.json()["ok"] is False


class TestKeepShowCoversUnlistedSeasons:
    """A show keep covers every season, so none of them may stay scheduled for deletion."""

    def _due(self, conn, media_id, status="pending"):
        insert_scheduled_action(
            conn,
            media_item_id=media_id,
            action="scheduled_deletion",
            token=f"tok-{media_id}",
            execute_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
            token_used=False,
            delete_status=status,
        )

    def _actions(self, conn):
        rows = conn.execute(
            "SELECT media_item_id, action FROM scheduled_actions ORDER BY media_item_id, id"
        ).fetchall()
        return [tuple(r) for r in rows]

    def test_keep_listing_one_season_stops_the_deletion_of_another(
        self, app_factory, authed_client, conn, tmp_path, monkeypatch
    ):
        from mediaman.scanner.deletions import DeletionExecutor

        root = tmp_path / "tv"
        monkeypatch.setenv("MEDIAMAN_DELETE_ROOTS", str(root))
        folders = []
        for number in (1, 2):
            folder = root / "Show" / f"Season {number}"
            folder.mkdir(parents=True)
            (folder / "ep.mkv").write_bytes(b"x")
            folders.append(folder)
            insert_media_item(
                conn,
                id=f"s{number}",
                title=f"Show S{number}",
                media_type="tv_season",
                plex_rating_key=f"s{number}",
                show_rating_key="show1",
                show_title="Show",
                season_number=number,
                file_path=str(folder),
            )
            self._due(conn, f"s{number}")
        client = authed_client(app_factory(kept_router, conn=conn), conn)

        resp = client.post(
            "/api/show/show1/keep", json={"duration": "forever", "season_ids": ["s1"]}
        )
        DeletionExecutor(conn=conn).execute()

        assert resp.status_code == 200
        assert all(folder.exists() for folder in folders)
        assert self._actions(conn) == [("s1", "protected_forever")]
        assert not conn.execute("SELECT 1 FROM audit_log WHERE action = 'deleted'").fetchone()

    def test_keep_cancels_only_its_own_shows_scheduled_deletions(
        self, app_factory, authed_client, conn
    ):
        for number in (1, 2, 3):
            _insert_season(conn, f"a{number}", "rk-A", "Show A", season=number)
        _insert_season(conn, "b1", "rk-B", "Show B", season=1)
        self._due(conn, "a1")
        insert_scheduled_action(
            conn,
            media_item_id="a2",
            action="snoozed",
            token="tok-a2",
            execute_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
        )
        self._due(conn, "a3")
        self._due(conn, "b1")
        client = authed_client(app_factory(kept_router, conn=conn), conn)

        resp = client.post(
            "/api/show/rk-A/keep", json={"duration": "forever", "season_ids": ["a1"]}
        )

        assert resp.status_code == 200
        assert resp.json()["deletion_running"] is False
        assert self._actions(conn) == [
            ("a1", "protected_forever"),
            ("a2", "snoozed"),
            ("b1", "scheduled_deletion"),
        ]

    def test_keep_leaves_a_running_deletion_of_a_season_it_does_not_list_and_says_so(
        self, app_factory, authed_client, conn
    ):
        for number in (1, 2):
            _insert_season(conn, f"a{number}", "rk-A", "Show A", season=number)
        self._due(conn, "a2", status="deleting")
        client = authed_client(app_factory(kept_router, conn=conn), conn)

        resp = client.post(
            "/api/show/rk-A/keep", json={"duration": "forever", "season_ids": ["a1"]}
        )

        assert resp.json()["deletion_running"] is True
        # The pass or crash recovery still needs this row to book the deletion it claimed.
        row = conn.execute(
            "SELECT delete_status FROM scheduled_actions "
            "WHERE media_item_id = 'a2' AND action = 'scheduled_deletion'"
        ).fetchone()
        assert row is not None and row["delete_status"] == "deleting"


class TestSeasonBadge:
    def test_kept_season_shows_the_shows_badge_and_season_suffix(
        self, app_factory, authed_client, conn
    ):
        _insert_season(conn, "s1", "show-1", "Some Show", season=2)
        _insert_protection(conn, "s1")
        client = authed_client(app_factory(kept_router, conn=conn), conn)

        item = client.get("/api/kept").json()["forever"][0]

        assert item["type_label"] == "TV \u00b7 S2"
        assert item["badge_class"] == "badge-tv"
