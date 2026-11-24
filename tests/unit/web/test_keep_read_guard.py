"""A change to a keep is refused when the keep is no longer the one the person read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import Header
from fastapi.testclient import TestClient

from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.repository.kept import fetch_show_kept_status
from mediaman.web.routes import kept, kept_show, library_api
from tests.helpers.factories import (
    insert_kept_show,
    insert_media_item,
    insert_scheduled_action,
)

SHOW = "/api/show/rk1"


@pytest.fixture(autouse=True)
def _fresh_limiters():
    limiters = (
        kept._UNPROTECT_LIMITER,
        kept_show._KEEP_SHOW_LIMITER,
        kept_show._REMOVE_SHOW_KEEP_LIMITER,
        library_api._KEEP_LIMITER,
    )
    for limiter in limiters:
        limiter.reset()
    yield
    for limiter in limiters:
        limiter.reset()


def _season(conn, number: int) -> None:
    insert_media_item(
        conn,
        id=f"s{number}",
        title=f"Night Shift S{number}",
        media_type="tv_season",
        plex_rating_key=f"s{number}",
        show_rating_key="rk1",
        show_title="Night Shift",
        season_number=number,
    )


@pytest.fixture
def admins(app_factory, conn):
    """Two admins, on a show of three seasons with no keep."""
    for number in (1, 2, 3):
        _season(conn, number)
    conn.commit()
    app = app_factory(kept.router, library_api.router, conn=conn)
    app.dependency_overrides[get_current_admin] = _named_admin
    return tuple(TestClient(app, headers={"x-admin": name}) for name in ("first", "second"))


def _named_admin(x_admin: Annotated[str, Header()]) -> str:
    return x_admin


def _read(keep: dict | None) -> dict | None:
    return keep and {k: keep[k] for k in ("action", "execute_at", "kept_at") if k in keep}


def _dialog(client) -> dict:
    """What the Keep dialog reads, as it sends it back."""
    body = client.get(f"{SHOW}/seasons").json()
    return {
        "show": _read(body["show_kept"]),
        "seasons": {s["id"]: _read(s["keep"]) for s in body["seasons"]},
    }


def _form(read: dict | None) -> dict:
    if read is None:
        return {"read_action": "none"}
    return {f"read_{k}": v for k, v in read.items() if v is not None}


def _keeps(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT media_item_id, action, snooze_duration FROM scheduled_actions ORDER BY id"
    )
    return {r["media_item_id"]: r["snooze_duration"] or r["action"] for r in rows}


def _show_keep(conn) -> str | None:
    row = conn.execute("SELECT snooze_duration FROM kept_shows").fetchone()
    return row and row["snooze_duration"]


def _changed(resp) -> bool:
    return resp.status_code == 409 and resp.json()["error"] == "keep_changed"


def _keep(client, season: str, duration: str, **read):
    return client.post(f"/api/media/{season}/keep", data={"duration": duration, **read})


class TestKeepingOneSeason:
    def test_a_keep_made_meanwhile_is_not_shortened(self, admins, conn):
        first, second = admins
        read = _dialog(first)
        _keep(second, "s1", "forever")

        assert _changed(_keep(first, "s1", "7 days", **_form(read["seasons"]["s1"])))
        assert _keeps(conn) == {"s1": "forever"}

    @pytest.mark.parametrize("said", [["kept_at"], []])
    def test_a_keep_lengthened_meanwhile_is_not_shortened(self, admins, conn, said):
        first, second = admins
        _keep(second, "s1", "30 days")
        read = _dialog(first)["seasons"]["s1"]
        _keep(second, "s1", "90 days")
        read = {k: read[k] for k in ("action", "execute_at", *said)}

        assert _changed(_keep(first, "s1", "7 days", **_form(read)))
        assert _keeps(conn) == {"s1": "90 days"}

    def test_an_unchanged_keep_is_changed_as_asked(self, admins, conn):
        first, _ = admins
        _keep(first, "s1", "forever")

        resp = _keep(first, "s1", "7 days", **_form(_dialog(first)["seasons"]["s1"]))

        assert resp.status_code == 200
        assert _keeps(conn) == {"s1": "7 days"}

    def test_a_keep_removed_meanwhile_is_not_taken_for_the_one_read(self, admins, conn):
        first, second = admins
        _keep(second, "s1", "30 days")
        read = _dialog(first)
        second.post("/api/media/s1/unprotect")

        assert _changed(_keep(first, "s1", "7 days", **_form(read["seasons"]["s1"])))
        assert _keeps(conn) == {}


class TestUndoOfAKeep:
    def test_it_removes_the_keep_the_save_made(self, admins, conn):
        first, _ = admins
        made = _keep(first, "s1", "30 days").json()["keep"]

        assert first.post("/api/media/s1/unprotect", json={"read": made}).status_code == 200
        assert _keeps(conn) == {}

    @pytest.mark.parametrize("duration", ["30 days", "forever"])
    def test_it_spares_the_keep_another_admin_made_since(self, admins, conn, duration):
        first, second = admins
        made = _keep(first, "s1", duration).json()["keep"]
        second.post("/api/media/s1/unprotect")
        _keep(second, "s1", duration)

        assert _changed(first.post("/api/media/s1/unprotect", json={"read": made}))
        assert _keeps(conn) == {"s1": duration}


class TestRemovingOneKeep:
    def test_a_keep_changed_since_it_was_read_stays(self, admins, conn):
        first, second = admins
        _keep(first, "s1", "7 days")
        read = _dialog(first)["seasons"]["s1"]
        _keep(second, "s1", "forever")

        assert _changed(first.post("/api/media/s1/unprotect", json={"read": read}))
        assert _keeps(conn) == {"s1": "forever"}


class TestKeepingTheShow:
    def _save(self, client, read, duration="7 days", seasons=("s1", "s2", "s3")):
        return client.post(
            f"{SHOW}/keep", json={"duration": duration, "season_ids": list(seasons), "read": read}
        )

    def test_a_show_keep_made_meanwhile_is_not_shortened(self, admins, conn):
        first, second = admins
        read = _dialog(first)
        self._save(second, _dialog(second), "forever")

        assert _changed(self._save(first, read))
        assert (_show_keep(conn), _keeps(conn)["s1"]) == ("forever", "forever")

    def test_a_season_keep_made_meanwhile_is_not_shortened(self, admins, conn):
        first, second = admins
        read = _dialog(first)
        _keep(second, "s2", "forever")

        assert _changed(self._save(first, read))
        assert (_show_keep(conn), _keeps(conn)) == (None, {"s2": "forever"})

    def test_a_longer_keep_it_was_to_spare_but_that_went_meanwhile_stops_it(self, admins, conn):
        first, second = admins
        _keep(second, "s1", "forever")
        read = _dialog(first)
        second.post("/api/media/s1/unprotect")

        assert _changed(self._save(first, read, seasons=("s2", "s3")))
        assert (_show_keep(conn), _keeps(conn)) == (None, {})

    def test_a_season_added_meanwhile_stops_it(self, admins, conn):
        first, _ = admins
        read = _dialog(first)
        _season(conn, 4)
        conn.commit()

        assert _changed(self._save(first, read))
        assert _show_keep(conn) is None

    def test_a_season_deleted_meanwhile_reads_as_changed(self, admins, conn):
        first, _ = admins
        read = _dialog(first)
        conn.execute("DELETE FROM media_items WHERE id = 's3'")
        conn.commit()

        assert _changed(self._save(first, read))

    def test_what_was_read_still_true_saves(self, admins, conn):
        first, _ = admins

        assert self._save(first, _dialog(first), "30 days").status_code == 200
        assert (_show_keep(conn), _keeps(conn)["s3"]) == ("30 days", "30 days")


class TestEndingTheShowKeep:
    """The show keep ends, the seasons left ticked are held and the unticked ones dropped."""

    @pytest.fixture
    def kept_show_of_three(self, admins, conn):
        first, second = admins
        first.post(f"{SHOW}/keep", json={"duration": "forever", "season_ids": ["s1", "s2", "s3"]})
        conn.execute("DELETE FROM scheduled_actions WHERE media_item_id = 's2'")
        conn.commit()
        return first, second

    def _end(self, client, read, drops):
        return client.post(f"{SHOW}/remove", json={"read": read, "drops": drops})

    def test_it_holds_every_other_season_in_the_same_change(self, kept_show_of_three, conn):
        first, _ = kept_show_of_three

        assert self._end(first, _dialog(first), ["s3"]).status_code == 200
        assert (_show_keep(conn), _keeps(conn)) == (None, {"s1": "forever", "s2": "forever"})

    def test_a_hold_lasts_at_least_as_long_as_the_show_keep(self, admins, conn):
        first, _ = admins
        insert_kept_show(
            conn,
            show_rating_key="rk1",
            action="snoozed",
            execute_at=(datetime.now(UTC) + timedelta(days=12)).isoformat(),
        )
        _keep(first, "s1", "90 days")
        _keep(first, "s2", "7 days")
        conn.commit()

        assert self._end(first, _dialog(first), ["s3"]).status_code == 200
        assert _keeps(conn) == {"s1": "90 days", "s2": "30 days"}
        assert [
            tuple(r)
            for r in conn.execute(
                "SELECT media_item_id, action FROM audit_log WHERE detail LIKE '%as the show keep%'"
            )
        ] == [("s2", "snoozed")]

    def test_a_show_keep_whose_end_cannot_be_read_is_held_forever(self, admins, conn):
        first, _ = admins
        insert_kept_show(conn, show_rating_key="rk1", action="snoozed", execute_at="unknown")
        conn.commit()

        assert self._end(first, _dialog(first), ["s3"]).status_code == 200
        assert _keeps(conn) == {"s1": "forever", "s2": "forever"}

    def test_a_show_keep_lengthened_meanwhile_stays(self, admins, conn):
        first, second = admins
        ids = ["s1", "s2", "s3"]
        first.post(f"{SHOW}/keep", json={"duration": "7 days", "season_ids": ids})
        read = _dialog(first)
        second.post(f"{SHOW}/keep", json={"duration": "forever", "season_ids": ids})

        assert _changed(self._end(first, read, ["s3"]))
        assert (_show_keep(conn), _keeps(conn)["s3"]) == ("forever", "forever")

    def test_a_show_keep_already_ended_changes_nothing(self, kept_show_of_three, conn):
        first, second = kept_show_of_three
        read = _dialog(first)
        second.post(f"{SHOW}/remove")

        assert _changed(self._end(first, read, ["s3"]))
        assert _keeps(conn)["s3"] == "forever"

    def test_a_season_added_meanwhile_is_not_left_without_a_keep(self, kept_show_of_three, conn):
        first, _ = kept_show_of_three
        read = _dialog(first)
        _season(conn, 4)
        conn.commit()

        assert _changed(self._end(first, read, ["s3"]))
        assert _show_keep(conn) == "forever"

    def test_a_season_whose_longer_keep_went_meanwhile_is_not_left_without_one(self, admins, conn):
        first, second = admins
        ids = ["s1", "s2", "s3"]
        first.post(f"{SHOW}/keep", json={"duration": "7 days", "season_ids": ids})
        _keep(first, "s1", "forever")
        read = _dialog(first)
        second.post("/api/media/s1/unprotect")

        assert _changed(self._end(first, read, ["s3"]))
        assert _show_keep(conn) == "7 days"

    def test_a_failure_part_way_leaves_every_keep(self, kept_show_of_three, conn, monkeypatch):
        first, _ = kept_show_of_three
        read = _dialog(first)

        def broken(*_args):
            raise RuntimeError("disk full")

        monkeypatch.setattr(kept_show, "delete_protections", broken)
        with pytest.raises(RuntimeError):
            self._end(first, read, ["s3"])
        assert (_show_keep(conn), _keeps(conn)) == ("forever", {"s1": "forever", "s3": "forever"})

    def test_seasons_are_dropped_only_with_what_was_read(self, kept_show_of_three, conn):
        first, _ = kept_show_of_three
        read = _dialog(first)

        assert first.post(f"{SHOW}/remove", json={"drops": ["s3"]}).status_code == 400
        del read["seasons"]["s3"]
        assert _changed(self._end(first, read, ["s3"]))
        assert (_show_keep(conn), _keeps(conn)["s3"]) == ("forever", "forever")

    def test_a_season_of_another_show_is_never_dropped(self, kept_show_of_three, conn):
        first, _ = kept_show_of_three
        insert_media_item(conn, id="film", plex_rating_key="film")
        insert_scheduled_action(
            conn, media_item_id="film", action="protected_forever", token="f", execute_at=None
        )
        conn.commit()

        assert self._end(first, _dialog(first), ["film"]).status_code == 400
        assert (_show_keep(conn), _keeps(conn)["film"]) == ("forever", "protected_forever")

    def test_a_season_already_being_deleted_is_held_and_said(self, kept_show_of_three, conn):
        first, _ = kept_show_of_three
        insert_scheduled_action(
            conn, media_item_id="s2", token="d", delete_status="deleting", token_used=False
        )
        conn.commit()

        body = self._end(first, _dialog(first), ["s3"]).json()

        assert body["deletion_running"] is True
        assert "already being deleted" in body["message"]
        assert _keeps(conn)["s2"] == "forever"


def test_a_lapsed_show_keep_is_no_keep(conn):
    now = datetime.now(UTC)
    for key, days in (("old", -1), ("new", 1)):
        insert_kept_show(
            conn,
            show_rating_key=key,
            action="snoozed",
            execute_at=(now + timedelta(days=days)).isoformat(),
        )

    assert fetch_show_kept_status(conn, "old") is None
    assert fetch_show_kept_status(conn, "new") is not None


def test_a_show_kept_again_is_kept_from_then(admins, conn):
    first, _ = admins
    for duration in ("30 days", "forever"):
        first.post(f"{SHOW}/keep", json={"duration": duration, "season_ids": ["s1", "s2"]})

    body = first.get(f"{SHOW}/seasons").json()

    assert body["show_kept"]["kept_at"] == body["seasons"][0]["keep"]["kept_at"]
